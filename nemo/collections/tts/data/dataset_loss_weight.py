# Copyright (c) 2023, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import math
import os
import pickle
import random
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import librosa
import numpy as np
import torch
from einops import rearrange
from tqdm import tqdm

from nemo.collections.asr.parts.preprocessing.features import WaveformFeaturizer
from nemo.collections.asr.parts.preprocessing.segment import AudioSegment
from nemo.collections.common.tokenizers.text_to_speech.tts_tokenizers import (
    BaseTokenizer,
    EnglishCharsTokenizer,
    EnglishPhonemesTokenizer,
)
from nemo.collections.tts.parts.utils.tts_dataset_utils import (
    BetaBinomialInterpolator,
    beta_binomial_prior_distribution,
    general_padding,
    get_base_dir,
)
from nemo.collections.tts.torch.tts_data_types import (
    DATA_STR2DATA_CLASS,
    MAIN_DATA_TYPES,
    AlignPriorMatrix,
    Durations,
    Energy,
    LMTokens,
    LogMel,
    LossWeight,
    P_voiced,
    Pitch,
    ReferenceAudio,
    SpeakerID,
    TTSDataType,
    Voiced_mask,
    WithLens,
)
from nemo.core.classes import Dataset
from nemo.utils import logging

try:
    from nemo_text_processing.text_normalization.normalize import Normalizer

    PYNINI_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    Normalizer = None
    PYNINI_AVAILABLE = False


EPSILON = 1e-9
WINDOW_FN_SUPPORTED = {
    'hann': torch.hann_window,
    'hamming': torch.hamming_window,
    'blackman': torch.blackman_window,
    'bartlett': torch.bartlett_window,
    'none': None,
}


class TTSDataset(Dataset):
    def __init__(
        self,
        manifest_filepath: Union[str, Path, List[str], List[Path]],
        sample_rate: int,
        text_tokenizer: Union[BaseTokenizer, Callable[[str], List[int]]],
        tokens: Optional[List[str]] = None,
        text_normalizer: Optional[Union[Normalizer, Callable[[str], str]]] = None,
        text_normalizer_call_kwargs: Optional[Dict] = None,
        text_tokenizer_pad_id: Optional[int] = None,
        sup_data_types: Optional[List[str]] = None,
        sup_data_path: Optional[Union[Path, str]] = None,
        max_duration: Optional[float] = None,
        min_duration: Optional[float] = None,
        ignore_file: Optional[Union[str, Path]] = None,
        trim: bool = False,
        trim_ref: Optional[float] = None,
        trim_top_db: Optional[int] = None,
        trim_frame_length: Optional[int] = None,
        trim_hop_length: Optional[int] = None,
        n_fft: int = 1024,
        win_length: Optional[int] = None,
        hop_length: Optional[int] = None,
        window: str = "hann",
        n_mels: int = 80,
        lowfreq: int = 0,
        highfreq: Optional[int] = None,
        segment_max_duration: Optional[int] = None,
        pitch_augment: bool = False,
        cache_pitch_augment: bool = True,
        pad_multiple: int = 1,
        **kwargs,
    ):
        """Dataset which can be used for training spectrogram generators and end-to-end TTS models.
        It loads main data types (audio, text) and specified supplementary data types (log mel, durations, align prior matrix, pitch, energy, speaker id).
        Some supplementary data types will be computed on the fly and saved in the sup_data_path if they did not exist before.
        Saved folder can be changed for some supplementary data types (see keyword args section).
        Arguments for supplementary data should be also specified in this class, and they will be used from kwargs (see keyword args section).
        Args:
            manifest_filepath (Union[str, Path, List[str], List[Path]]): Path(s) to the .json manifests containing information on the
                dataset. Each line in the .json file should be valid json. Note: the .json file itself is not valid
                json. Each line should contain the following:
                    "audio_filepath": <PATH_TO_WAV>,
                    "text": <THE_TRANSCRIPT>,
                    "normalized_text": <NORMALIZED_TRANSCRIPT> (Optional),
                    "mel_filepath": <PATH_TO_LOG_MEL_PT> (Optional),
                    "duration": <Duration of audio clip in seconds> (Optional),
            sample_rate (int): The sample rate of the audio. Or the sample rate that we will resample all files to.
            text_tokenizer (Optional[Union[BaseTokenizer, Callable[[str], List[int]]]]): BaseTokenizer or callable which represents text tokenizer.
            tokens (Optional[List[str]]): Tokens from text_tokenizer. Should be specified if text_tokenizer is not BaseTokenizer.
            text_normalizer (Optional[Union[Normalizer, Callable[[str], str]]]): Normalizer or callable which represents text normalizer.
            text_normalizer_call_kwargs (Optional[Dict]): Additional arguments for text_normalizer function.
            text_tokenizer_pad_id (Optional[int]): Index of padding. Should be specified if text_tokenizer is not BaseTokenizer.
            sup_data_types (Optional[List[str]]): List of supplementary data types.
            sup_data_path (Optional[Union[Path, str]]): A folder that contains or will contain supplementary data (e.g. pitch).
            max_duration (Optional[float]): Max duration of audio clips in seconds. All samples exceeding this will be
                pruned prior to training. Note: Requires "duration" to be set in the manifest file. It does not load
                audio to compute duration. Defaults to None which does not prune.
            min_duration (Optional[float]): Min duration of audio clips in seconds. All samples lower than this will be
                pruned prior to training. Note: Requires "duration" to be set in the manifest file. It does not load
                audio to compute duration. Defaults to None which does not prune.
            ignore_file (Optional[Union[str, Path]]): The location of a pickle-saved list of audio paths
                that will be pruned prior to training. Defaults to None which does not prune.
            trim (bool): Whether to apply `librosa.effects.trim` to trim leading and trailing silence from an audio
                signal. Defaults to False.
            trim_ref (Optional[float]): the reference amplitude. By default, it uses `np.max` and compares to the peak
                amplitude in the signal.
            trim_top_db (Optional[int]): the threshold (in decibels) below reference to consider as silence.
                Defaults to 60.
            trim_frame_length (Optional[int]): the number of samples per analysis frame. Defaults to 2048.
            trim_hop_length (Optional[int]): the number of samples between analysis frames. Defaults to 512.
            n_fft (int): The number of fft samples. Defaults to 1024
            win_length (Optional[int]): The length of the stft windows. Defaults to None which uses n_fft.
            hop_length (Optional[int]): The hope length between fft computations. Defaults to None which uses n_fft//4.
            window (str): One of 'hann', 'hamming', 'blackman','bartlett', 'none'. Which corresponds to the
                equivalent torch window function.
            n_mels (int): The number of mel filters. Defaults to 80.
            lowfreq (int): The lowfreq input to the mel filter calculation. Defaults to 0.
            highfreq (Optional[int]): The highfreq input to the mel filter calculation. Defaults to None.
        Keyword Args:
            log_mel_folder (Optional[Union[Path, str]]): The folder that contains or will contain log mel spectrograms.
            pitch_folder (Optional[Union[Path, str]]): The folder that contains or will contain pitch.
            voiced_mask_folder (Optional[Union[Path, str]]): The folder that contains or will contain voiced mask of the pitch
            p_voiced_folder (Optional[Union[Path, str]]): The folder that contains or will contain p_voiced(probability) of the pitch
            energy_folder (Optional[Union[Path, str]]): The folder that contains or will contain energy.
            durs_file (Optional[str]): String path to pickled durations location.
            durs_type (Optional[str]): Type of durations. Currently, supported only "aligner-based".
            use_beta_binomial_interpolator (Optional[bool]): Whether to use beta-binomial interpolator for calculating alignment prior matrix. Defaults to False.
            pitch_fmin (Optional[float]): The fmin input to librosa.pyin. Defaults to librosa.note_to_hz('C2').
            pitch_fmax (Optional[float]): The fmax input to librosa.pyin. Defaults to librosa.note_to_hz('C7').
            pitch_mean (Optional[float]): The mean that we use to normalize the pitch.
            pitch_std (Optional[float]): The std that we use to normalize the pitch.
            segment_max_duration (Optional[float]): If audio length is greater than segment_max_duration, take a random segment of segment_max_duration (Used for SV task in SSLDisentangler)
            pitch_augment (bool): Whether to apply pitch-shift transform and return a pitch-shifted audio. If set as False, audio_shifted will be None (used in SSLDisentangler)
            cache_pitch_augment (bool): Whether to cache pitch augmented audio or not. Defaults to False (used in SSLDisentangler)
            pad_multiple (int): If audio length is not divisible by pad_multiple, pad the audio with zeros to make it divisible by pad_multiple (used in SSLDisentangler)
            pitch_norm (Optional[bool]): Whether to normalize pitch or not. If True, requires providing either
                pitch_stats_path or (pitch_mean and pitch_std).
            pitch_stats_path (Optional[Path, str]): Path to file containing speaker level pitch statistics.
            reference_audio_type (Optional[str]): Criterion for the selection of reference audios for the GlobalStyleToken submodule. Currently, supported values are "ground-truth" (reference audio = ground truth audio, like in the original GST paper) and "same-speaker" (reference audio = random audio from the same speaker). Defaults to "same-speaker".
        """
        super().__init__()

        # Initialize text tokenizer
        self.text_tokenizer = text_tokenizer

        self.phoneme_probability = None
        if isinstance(self.text_tokenizer, BaseTokenizer):
            self.text_tokenizer_pad_id = text_tokenizer.pad
            self.phoneme_probability = getattr(self.text_tokenizer, "phoneme_probability", None)
        else:
            if text_tokenizer_pad_id is None:
                raise ValueError(f"text_tokenizer_pad_id must be specified if text_tokenizer is not BaseTokenizer")

            if tokens is None:
                raise ValueError(f"tokens must be specified if text_tokenizer is not BaseTokenizer")

            self.text_tokenizer_pad_id = text_tokenizer_pad_id
        self.cache_text = True if self.phoneme_probability is None else False

        # Initialize text normalizer if specified
        self.text_normalizer = text_normalizer
        if self.text_normalizer is None:
            self.text_normalizer_call = None
        elif not PYNINI_AVAILABLE:
            raise ImportError(
                "`nemo_text_processing` is not installed, see https://github.com/NVIDIA/NeMo-text-processing for details"
            )
        else:
            self.text_normalizer_call = (
                self.text_normalizer.normalize
                if isinstance(self.text_normalizer, Normalizer)
                else self.text_normalizer
            )
        self.text_normalizer_call_kwargs = (
            text_normalizer_call_kwargs if text_normalizer_call_kwargs is not None else {}
        )

        # Initialize and read manifest file(s), filter out data by duration and ignore_file, compute base dir
        if isinstance(manifest_filepath, str):
            manifest_filepath = [manifest_filepath]
        self.manifest_filepath = manifest_filepath
        self.lengths = []  # Needed for BucketSampling

        data = []
        total_duration = 0
        for manifest_file in self.manifest_filepath:
            with open(Path(manifest_file).expanduser(), 'r') as f:
                logging.info(f"Loading dataset from {manifest_file}.")
                for line in tqdm(f):
                    item = json.loads(line)

                    file_info = {
                        "audio_filepath": item["audio_filepath"],
                        "original_text": item["text"],
                        "mel_filepath": item["mel_filepath"] if "mel_filepath" in item else None,
                        "duration": item["duration"] if "duration" in item else None,
                        "speaker_id": item["speaker"] if "speaker" in item else None,
                    }

                    if "normalized_text" in item:
                        file_info["normalized_text"] = item["normalized_text"]
                    elif "text_normalized" in item:
                        file_info["normalized_text"] = item["text_normalized"]
                    else:
                        text = item["text"]
                        if self.text_normalizer is not None:
                            text = self.text_normalizer_call(text, **self.text_normalizer_call_kwargs)
                        file_info["normalized_text"] = text

                    if self.cache_text:
                        file_info["text_tokens"] = self.text_tokenizer(file_info["normalized_text"])

                    data.append(file_info)
                    # Calculating length of spectrogram from input audio for batch sampling
                    self.lengths.append(os.path.getsize(item["audio_filepath"]) // (n_fft // 2))

                    if file_info["duration"] is None:
                        logging.info(
                            "Not all audio files have duration information. Duration logging will be disabled."
                        )
                        total_duration = None

                    if total_duration is not None:
                        total_duration += item["duration"]

        logging.info(f"Loaded dataset with {len(data)} files.")
        if total_duration is not None:
            logging.info(f"Dataset contains {total_duration / 3600:.2f} hours.")

        self.data = TTSDataset.filter_files(data, ignore_file, min_duration, max_duration, total_duration)
        self.base_data_dir = get_base_dir([item["audio_filepath"] for item in self.data])

        # Initialize audio and mel related parameters
        self.sample_rate = sample_rate
        self.featurizer = WaveformFeaturizer(sample_rate=self.sample_rate)
        self.trim = trim
        self.trim_ref = trim_ref if trim_ref is not None else np.max
        self.trim_top_db = trim_top_db if trim_top_db is not None else 60
        self.trim_frame_length = trim_frame_length if trim_frame_length is not None else 2048
        self.trim_hop_length = trim_hop_length if trim_hop_length is not None else 512

        self.segment_max_duration = segment_max_duration
        self.pitch_augment = pitch_augment
        self.cache_pitch_augment = cache_pitch_augment

        self.n_fft = n_fft
        self.n_mels = n_mels
        self.lowfreq = lowfreq
        self.highfreq = highfreq
        self.window = window
        self.win_length = win_length or self.n_fft
        self.hop_length = hop_length
        self.hop_len = self.hop_length or self.n_fft // 4
        self.fb = torch.tensor(
            librosa.filters.mel(
                sr=self.sample_rate, n_fft=self.n_fft, n_mels=self.n_mels, fmin=self.lowfreq, fmax=self.highfreq
            ),
            dtype=torch.float,
        ).unsqueeze(0)

        try:
            window_fn = WINDOW_FN_SUPPORTED[self.window]
        except KeyError:
            raise NotImplementedError(
                f"Current implementation doesn't support {self.window} window. "
                f"Please choose one from {list(WINDOW_FN_SUPPORTED.keys())}."
            )

        self.stft = lambda x: torch.stft(
            input=x,
            n_fft=self.n_fft,
            hop_length=self.hop_len,
            win_length=self.win_length,
            window=window_fn(self.win_length, periodic=False).to(torch.float) if window_fn else None,
            return_complex=True,
        )

        # Initialize sup_data_path, sup_data_types and run preprocessing methods for every supplementary data type
        if sup_data_path is not None:
            Path(sup_data_path).mkdir(parents=True, exist_ok=True)
            self.sup_data_path = sup_data_path

        self.sup_data_types = []
        if sup_data_types is not None:
            for d_as_str in sup_data_types:
                try:
                    sup_data_type = DATA_STR2DATA_CLASS[d_as_str]
                except KeyError:
                    raise NotImplementedError(f"Current implementation doesn't support {d_as_str} type.")

                self.sup_data_types.append(sup_data_type)

            if ("voiced_mask" in sup_data_types or "p_voiced" in sup_data_types) and ("pitch" not in sup_data_types):
                raise ValueError(
                    "Please add 'pitch' to sup_data_types in YAML because 'pitch' is required when using either "
                    "'voiced_mask' or 'p_voiced' or both."
                )

        self.sup_data_types_set = set(self.sup_data_types)

        for data_type in self.sup_data_types:
            getattr(self, f"add_{data_type.name}")(**kwargs)

        self.pad_multiple = pad_multiple

    @staticmethod
    def filter_files(data, ignore_file, min_duration, max_duration, total_duration):
        if ignore_file:
            logging.info(f"Using {ignore_file} to prune dataset.")
            with open(Path(ignore_file).expanduser(), "rb") as f:
                wavs_to_ignore = set(pickle.load(f))

        filtered_data: List[Dict] = []
        pruned_duration = 0 if total_duration is not None else None
        pruned_items = 0
        for item in data:
            audio_path = item['audio_filepath']

            # Prune data according to min/max_duration & the ignore file
            if total_duration is not None:
                if (min_duration and item["duration"] < min_duration) or (
                    max_duration and item["duration"] > max_duration
                ):
                    pruned_duration += item["duration"]
                    pruned_items += 1
                    continue

            if ignore_file and (audio_path in wavs_to_ignore):
                pruned_items += 1
                pruned_duration += item["duration"]
                wavs_to_ignore.remove(audio_path)
                continue

            filtered_data.append(item)

        logging.info(f"Pruned {pruned_items} files. Final dataset contains {len(filtered_data)} files")
        if pruned_duration is not None:
            logging.info(
                f"Pruned {pruned_duration / 3600:.2f} hours. Final dataset contains "
                f"{(total_duration - pruned_duration) / 3600:.2f} hours."
            )

        return filtered_data

    def add_log_mel(self, **kwargs):
        self.log_mel_folder = kwargs.pop('log_mel_folder', None)

        if self.log_mel_folder is None:
            self.log_mel_folder = Path(self.sup_data_path) / LogMel.name
        elif isinstance(self.log_mel_folder, str):
            self.log_mel_folder = Path(self.log_mel_folder)

        self.log_mel_folder.mkdir(exist_ok=True, parents=True)

    def add_durations(self, **kwargs):
        durs_file = kwargs.pop('durs_file')
        durs_type = kwargs.pop('durs_type')

        audio_stem2durs = torch.load(durs_file)
        self.durs = []

        for tag in [Path(d["audio_filepath"]).stem for d in self.data]:
            durs = audio_stem2durs[tag]
            if durs_type == "aligner-based":
                self.durs.append(durs)
            else:
                raise NotImplementedError(
                    f"{durs_type} duration type is not supported. Only aligner-based is supported at this moment."
                )

    def add_align_prior_matrix(self, **kwargs):
        self.use_beta_binomial_interpolator = kwargs.pop('use_beta_binomial_interpolator', False)
        if not self.cache_text:
            if 'use_beta_binomial_interpolator' in kwargs and not self.use_beta_binomial_interpolator:
                logging.warning(
                    "phoneme_probability is not None, but use_beta_binomial_interpolator=False, we"
                    " set use_beta_binomial_interpolator=True manually to use phoneme_probability."
                )
            self.use_beta_binomial_interpolator = True

        if self.use_beta_binomial_interpolator:
            self.beta_binomial_interpolator = BetaBinomialInterpolator()

    def add_pitch(self, **kwargs):
        self.pitch_folder = kwargs.pop('pitch_folder', None)

        if self.pitch_folder is None:
            self.pitch_folder = Path(self.sup_data_path) / Pitch.name
        elif isinstance(self.pitch_folder, str):
            self.pitch_folder = Path(self.pitch_folder)

        self.pitch_folder.mkdir(exist_ok=True, parents=True)

        self.pitch_fmin = kwargs.pop("pitch_fmin", librosa.note_to_hz('C2'))
        self.pitch_fmax = kwargs.pop("pitch_fmax", librosa.note_to_hz('C7'))
        self.pitch_mean = kwargs.pop("pitch_mean", None)
        self.pitch_std = kwargs.pop("pitch_std", None)
        self.pitch_norm = kwargs.pop("pitch_norm", False)
        pitch_stats_path = kwargs.pop("pitch_stats_path", None)

        if self.pitch_norm:
            # XOR to validate that both or neither pitch mean and std are provided
            assert (self.pitch_mean is None) == (
                self.pitch_std is None
            ), f"Found only 1 of (pitch_mean, pitch_std): ({self.pitch_mean}, {self.pitch_std})"

            # XOR to validate that exactly 1 of (pitch_mean, pitch_std) or pitch_stats_path is provided.
            assert (self.pitch_mean is None) != (pitch_stats_path is None), (
                f"pitch_norm requires exactly 1 of (pitch_mean, pitch_std) or pitch_stats_path. "
                f"Provided: ({self.pitch_mean}, {self.pitch_std}) and {pitch_stats_path}"
            )

        if pitch_stats_path is not None:
            with open(Path(pitch_stats_path), 'r', encoding="utf-8") as pitch_f:
                self.pitch_stats = json.load(pitch_f)

    # saving voiced_mask and p_voiced with pitch
    def add_voiced_mask(self, **kwargs):
        self.voiced_mask_folder = kwargs.pop('voiced_mask_folder', None)

        if self.voiced_mask_folder is None:
            self.voiced_mask_folder = Path(self.sup_data_path) / Voiced_mask.name

        self.voiced_mask_folder.mkdir(exist_ok=True, parents=True)

    def add_p_voiced(self, **kwargs):
        self.p_voiced_folder = kwargs.pop('p_voiced_folder', None)

        if self.p_voiced_folder is None:
            self.p_voiced_folder = Path(self.sup_data_path) / P_voiced.name

        self.p_voiced_folder.mkdir(exist_ok=True, parents=True)

    def add_energy(self, **kwargs):
        self.energy_folder = kwargs.pop('energy_folder', None)

        if self.energy_folder is None:
            self.energy_folder = Path(self.sup_data_path) / Energy.name
        elif isinstance(self.energy_folder, str):
            self.energy_folder = Path(self.energy_folder)

        self.energy_folder.mkdir(exist_ok=True, parents=True)

    def add_speaker_id(self, **kwargs):
        pass

    def add_reference_audio(self, **kwargs):
        reference_audio_type = kwargs.pop("reference_audio_type", "same-speaker")
        if reference_audio_type == "same-speaker":
            assert SpeakerID in self.sup_data_types, "Please add speaker_id in sup_data_types."
            # Add a mapping for each speaker to their manifest indexes
            speaker_to_index_map = defaultdict(set)
            for i, d in enumerate(self.data):
                speaker_to_index_map[d["speaker_id"]].add(i)
            # Random sample a reference audio from the same speaker
            self.get_reference_for_sample = lambda sample: self.data[
                random.sample(speaker_to_index_map[sample["speaker_id"]], 1)[0]
            ]
        elif reference_audio_type == "ground-truth":
            # Use ground truth audio as reference audio
            self.get_reference_for_sample = lambda sample: sample
        else:
            raise NotImplementedError(f"Reference audio type \"{reference_audio_type}\" is not supported.")

    def get_spec(self, audio):
        with torch.cuda.amp.autocast(enabled=False):
            spec = self.stft(audio)
            if spec.dtype in [torch.cfloat, torch.cdouble]:
                spec = torch.view_as_real(spec)
            spec = torch.sqrt(spec.pow(2).sum(-1) + EPSILON)
        return spec

    def get_log_mel(self, audio):
        with torch.cuda.amp.autocast(enabled=False):
            spec = self.get_spec(audio)
            mel = torch.matmul(self.fb.to(spec.dtype), spec)
            log_mel = torch.log(torch.clamp(mel, min=torch.finfo(mel.dtype).tiny))
        return log_mel

    def pitch_shift(self, audio, sr, rel_audio_path_as_text_id):
        audio_shifted_path = Path(self.sup_data_path) / f"{rel_audio_path_as_text_id}_pitch_shift.pt"
        if audio_shifted_path.exists() and self.cache_pitch_augment:
            audio_shifted = torch.load(audio_shifted_path)
            return audio_shifted
        else:
            choice1 = np.random.uniform(-4, -1)
            choice2 = np.random.uniform(1, 4)
            shift_val = random.choice([choice1, choice2])
            audio_shifted = librosa.effects.pitch_shift(audio, sr=sr, n_steps=shift_val)
            # save audio_shifted
            audio_shifted = torch.tensor(audio_shifted)
            if self.cache_pitch_augment:
                torch.save(audio_shifted, audio_shifted_path)
            return audio_shifted

    def _pad_wav_to_multiple(self, wav):
        if self.pad_multiple > 1:
            if wav.shape[0] % self.pad_multiple != 0:
                wav = torch.cat(
                    [wav, torch.zeros(self.pad_multiple - wav.shape[0] % self.pad_multiple, dtype=torch.float)]
                )
        return wav

    def __getitem__(self, index):
        sample = self.data[index]

        # Let's keep audio name and all internal directories in rel_audio_path_as_text_id to avoid any collisions
        rel_audio_path = Path(sample["audio_filepath"]).relative_to(self.base_data_dir).with_suffix("")
        rel_audio_path_as_text_id = str(rel_audio_path).replace("/", "_")

        if (
            self.segment_max_duration is not None
            and 'duration' in sample
            and sample['duration'] > self.segment_max_duration
        ):
            # this case has been added for segmenting audio for speaker verification task of SSLDisentangler
            n_segments = int(self.segment_max_duration * self.sample_rate)
            features = AudioSegment.segment_from_file(
                sample["audio_filepath"], target_sr=self.sample_rate, n_segments=n_segments, trim=self.trim
            )
            audio_shifted = None

            # should not have pitch shift augmented data for speaker verification
            assert not self.pitch_augment

            features = torch.tensor(features.samples)
            if self.pad_multiple > 1:
                features = self._pad_wav_to_multiple(features)
            audio, audio_length = features, torch.tensor(features.shape[0]).long()
        else:
            features = self.featurizer.process(
                sample["audio_filepath"],
                trim=self.trim,
                trim_ref=self.trim_ref,
                trim_top_db=self.trim_top_db,
                trim_frame_length=self.trim_frame_length,
                trim_hop_length=self.trim_hop_length,
            )

            if self.pad_multiple > 1:
                features = self._pad_wav_to_multiple(features)
            audio_shifted = None
            if self.pitch_augment:
                audio_shifted = self.pitch_shift(
                    features.cpu().detach().numpy(), self.sample_rate, rel_audio_path_as_text_id
                )
                assert audio_shifted.size() == features.size(), "{} != {}".format(
                    audio_shifted.size(), features.size()
                )

            audio, audio_length = features, torch.tensor(features.shape[0]).long()

        if "text_tokens" in sample:
            text = torch.tensor(sample["text_tokens"]).long()
            text_length = torch.tensor(len(text)).long()
        else:
            tokenized = self.text_tokenizer(sample["normalized_text"])
            text = torch.tensor(tokenized).long()
            text_length = torch.tensor(len(tokenized)).long()

        # Load mel if needed
        log_mel, log_mel_length = None, None
        if LogMel in self.sup_data_types_set:
            mel_path = sample["mel_filepath"]

            if mel_path is not None and Path(mel_path).exists():
                log_mel = torch.load(mel_path)
            else:
                mel_path = self.log_mel_folder / f"{rel_audio_path_as_text_id}.pt"

                if mel_path.exists():
                    log_mel = torch.load(mel_path)
                else:
                    log_mel = self.get_log_mel(audio)
                    torch.save(log_mel, mel_path)

            log_mel = log_mel.squeeze(0)
            log_mel_length = torch.tensor(log_mel.shape[1]).long()

        # Load durations if needed
        durations = None
        if Durations in self.sup_data_types_set:
            durations = self.durs[index]

        # Load alignment prior matrix if needed
        align_prior_matrix = None
        if AlignPriorMatrix in self.sup_data_types_set:
            mel_len = self.get_log_mel(audio).shape[2]
            if self.use_beta_binomial_interpolator:
                align_prior_matrix = torch.from_numpy(self.beta_binomial_interpolator(mel_len, text_length.item()))
            else:
                align_prior_matrix = torch.from_numpy(beta_binomial_prior_distribution(text_length, mel_len))

        non_exist_voiced_index = []
        my_var = locals()
        for i, voiced_item in enumerate([Pitch, Voiced_mask, P_voiced]):
            if voiced_item in self.sup_data_types_set:
                voiced_folder = getattr(self, f"{voiced_item.name}_folder")
                voiced_filepath = voiced_folder / f"{rel_audio_path_as_text_id}.pt"
                if voiced_filepath.exists():
                    my_var.__setitem__(voiced_item.name, torch.load(voiced_filepath).float())
                else:
                    non_exist_voiced_index.append((i, voiced_item.name, voiced_filepath))

        if len(non_exist_voiced_index) != 0:
            voiced_tuple = librosa.pyin(
                audio.numpy(),
                fmin=self.pitch_fmin,
                fmax=self.pitch_fmax,
                frame_length=self.win_length,
                sr=self.sample_rate,
                fill_na=0.0,
            )
            for (i, voiced_name, voiced_filepath) in non_exist_voiced_index:
                my_var.__setitem__(voiced_name, torch.from_numpy(voiced_tuple[i]).float())
                torch.save(my_var.get(voiced_name), voiced_filepath)

        pitch = my_var.get('pitch', None)
        pitch_length = my_var.get('pitch_length', None)
        voiced_mask = my_var.get('voiced_mask', None)
        p_voiced = my_var.get('p_voiced', None)

        # normalize pitch if requested.
        if pitch is not None:
            pitch_length = torch.tensor(len(pitch)).long()
            if self.pitch_norm:
                if self.pitch_mean is not None and self.pitch_std is not None:
                    sample_pitch_mean = self.pitch_mean
                    sample_pitch_std = self.pitch_std
                elif self.pitch_stats:
                    if "speaker_id" in sample and str(sample["speaker_id"]) in self.pitch_stats:
                        pitch_stats = self.pitch_stats[str(sample["speaker_id"])]
                    elif "default" in self.pitch_stats:
                        pitch_stats = self.pitch_stats["default"]
                    else:
                        raise ValueError(f"Could not find pitch stats for {sample}.")
                    sample_pitch_mean = pitch_stats["pitch_mean"]
                    sample_pitch_std = pitch_stats["pitch_std"]
                else:
                    raise ValueError(f"Missing statistics for pitch normalization.")

                pitch -= sample_pitch_mean
                pitch[pitch == -sample_pitch_mean] = 0.0  # Zero out values that were previously zero
                pitch /= sample_pitch_std

        # Load energy if needed
        energy, energy_length = None, None
        if Energy in self.sup_data_types_set:
            energy_path = self.energy_folder / f"{rel_audio_path_as_text_id}.pt"

            if energy_path.exists():
                energy = torch.load(energy_path).float()
            else:
                spec = self.get_spec(audio)
                energy = torch.linalg.norm(spec.squeeze(0), axis=0).float()
                torch.save(energy, energy_path)

            energy_length = torch.tensor(len(energy)).long()

        # Load speaker id if needed
        speaker_id = None
        if SpeakerID in self.sup_data_types_set:
            speaker_id = torch.tensor(sample["speaker_id"]).long()

        reference_audio, reference_audio_length = None, None
        if ReferenceAudio in self.sup_data_types_set:
            reference = self.get_reference_for_sample(sample)
            reference_audio = self.featurizer.process(
                reference["audio_filepath"],
                trim=self.trim,
                trim_ref=self.trim_ref,
                trim_top_db=self.trim_top_db,
                trim_frame_length=self.trim_frame_length,
                trim_hop_length=self.trim_hop_length,
            )
            reference_audio_length = torch.tensor(reference_audio.shape[0]).long()

        loss_weight = None
        if LossWeight in self.sup_data_types_set:
            loss_weight = torch.tensor(sample["loss_weight"]).float()

        return (
            audio,
            audio_length,
            text,
            text_length,
            log_mel,
            log_mel_length,
            durations,
            align_prior_matrix,
            pitch,
            pitch_length,
            energy,
            energy_length,
            speaker_id,
            voiced_mask,
            p_voiced,
            audio_shifted,
            reference_audio,
            reference_audio_length,
            loss_weight,
        )

    def __len__(self):
        return len(self.data)

    def join_data(self, data_dict):
        result = []
        for data_type in MAIN_DATA_TYPES + self.sup_data_types:
            result.append(data_dict[data_type.name])

            if issubclass(data_type, TTSDataType) and issubclass(data_type, WithLens):
                result.append(data_dict[f"{data_type.name}_lens"])

        return tuple(result)

    def general_collate_fn(self, batch):
        (
            _,
            audio_lengths,
            _,
            tokens_lengths,
            _,
            log_mel_lengths,
            durations_list,
            align_prior_matrices_list,
            pitches,
            pitches_lengths,
            energies,
            energies_lengths,
            _,
            voiced_masks,
            p_voiceds,
            _,
            _,
            reference_audio_lengths,
        ) = zip(*batch)

        max_audio_len = max(audio_lengths).item()
        max_tokens_len = max(tokens_lengths).item()
        max_log_mel_len = max(log_mel_lengths) if LogMel in self.sup_data_types_set else None
        max_durations_len = max([len(i) for i in durations_list]) if Durations in self.sup_data_types_set else None
        max_pitches_len = max(pitches_lengths).item() if Pitch in self.sup_data_types_set else None
        max_energies_len = max(energies_lengths).item() if Energy in self.sup_data_types_set else None
        max_reference_audio_len = (
            max(reference_audio_lengths).item() if ReferenceAudio in self.sup_data_types_set else None
        )

        if LogMel in self.sup_data_types_set:
            log_mel_pad = torch.finfo(batch[0][4].dtype).tiny

        align_prior_matrices = (
            torch.zeros(
                len(align_prior_matrices_list),
                max([prior_i.shape[0] for prior_i in align_prior_matrices_list]),
                max([prior_i.shape[1] for prior_i in align_prior_matrices_list]),
            )
            if AlignPriorMatrix in self.sup_data_types_set
            else []
        )
        (
            audios,
            tokens,
            log_mels,
            durations_list,
            pitches,
            energies,
            speaker_ids,
            voiced_masks,
            p_voiceds,
            audios_shifted,
            reference_audios,
            loss_weights,
        ) = (
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
            [],
        )

        for i, sample_tuple in enumerate(batch):
            (
                audio,
                audio_len,
                token,
                token_len,
                log_mel,
                log_mel_len,
                durations,
                align_prior_matrix,
                pitch,
                pitch_length,
                energy,
                energy_length,
                speaker_id,
                voiced_mask,
                p_voiced,
                audio_shifted,
                reference_audio,
                reference_audios_length,
                loss_weight,
            ) = sample_tuple

            audio = general_padding(audio, audio_len.item(), max_audio_len)
            audios.append(audio)

            token = general_padding(token, token_len.item(), max_tokens_len, pad_value=self.text_tokenizer_pad_id)
            tokens.append(token)

            if audio_shifted is not None:
                audio_shifted = general_padding(audio_shifted, audio_len.item(), max_audio_len)
                audios_shifted.append(audio_shifted)

            if LogMel in self.sup_data_types_set:
                log_mels.append(general_padding(log_mel, log_mel_len, max_log_mel_len, pad_value=log_mel_pad))

            if Durations in self.sup_data_types_set:
                durations_list.append(general_padding(durations, len(durations), max_durations_len))

            if AlignPriorMatrix in self.sup_data_types_set:
                align_prior_matrices[
                    i, : align_prior_matrix.shape[0], : align_prior_matrix.shape[1]
                ] = align_prior_matrix

            if Pitch in self.sup_data_types_set:
                pitches.append(general_padding(pitch, pitch_length.item(), max_pitches_len))

            if Voiced_mask in self.sup_data_types_set:
                voiced_masks.append(general_padding(voiced_mask, pitch_length.item(), max_pitches_len))

            if P_voiced in self.sup_data_types_set:
                p_voiceds.append(general_padding(p_voiced, pitch_length.item(), max_pitches_len))

            if Energy in self.sup_data_types_set:
                energies.append(general_padding(energy, energy_length.item(), max_energies_len))

            if SpeakerID in self.sup_data_types_set:
                speaker_ids.append(speaker_id)

            if ReferenceAudio in self.sup_data_types_set:
                reference_audios.append(
                    general_padding(reference_audio, reference_audios_length.item(), max_reference_audio_len)
                )

            if LossWeight in self.sup_data_types_set:
                loss_weights.append(loss_weight)

        data_dict = {
            "audio": torch.stack(audios),
            "audio_lens": torch.stack(audio_lengths),
            "text": torch.stack(tokens),
            "text_lens": torch.stack(tokens_lengths),
            "log_mel": torch.stack(log_mels) if LogMel in self.sup_data_types_set else None,
            "log_mel_lens": torch.stack(log_mel_lengths) if LogMel in self.sup_data_types_set else None,
            "durations": torch.stack(durations_list) if Durations in self.sup_data_types_set else None,
            "align_prior_matrix": align_prior_matrices if AlignPriorMatrix in self.sup_data_types_set else None,
            "pitch": torch.stack(pitches) if Pitch in self.sup_data_types_set else None,
            "pitch_lens": torch.stack(pitches_lengths) if Pitch in self.sup_data_types_set else None,
            "energy": torch.stack(energies) if Energy in self.sup_data_types_set else None,
            "energy_lens": torch.stack(energies_lengths) if Energy in self.sup_data_types_set else None,
            "speaker_id": torch.stack(speaker_ids) if SpeakerID in self.sup_data_types_set else None,
            "voiced_mask": torch.stack(voiced_masks) if Voiced_mask in self.sup_data_types_set else None,
            "p_voiced": torch.stack(p_voiceds) if P_voiced in self.sup_data_types_set else None,
            "audio_shifted": torch.stack(audios_shifted) if audio_shifted is not None else None,
            "reference_audio": torch.stack(reference_audios) if ReferenceAudio in self.sup_data_types_set else None,
            "reference_audio_lens": torch.stack(reference_audio_lengths)
            if ReferenceAudio in self.sup_data_types_set
            else None,
            "loss_weight": torch.stack(loss_weights) if LossWeight in self.sup_data_types_set else None,
        }

        return data_dict

    def _collate_fn(self, batch):
        data_dict = self.general_collate_fn(batch)
        joined_data = self.join_data(data_dict)
        return joined_data
