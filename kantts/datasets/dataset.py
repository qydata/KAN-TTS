import os
import torch
import glob
import logging
from multiprocessing import Manager
import librosa
import numpy as np
import random
import functools
from tqdm import tqdm
import math
from kantts.utils.ling_unit.ling_unit import KanTtsLinguisticUnit
from scipy.stats import betabinom


@functools.lru_cache(maxsize=256)
def beta_binomial_prior_distribution(phoneme_count, mel_count, scaling=1.0):
    P = phoneme_count
    M = mel_count
    x = np.arange(0, P)
    mel_text_probs = []
    for i in range(1, M + 1):
        a, b = scaling * i, scaling * (M + 1 - i)
        rv = betabinom(P, a, b)
        mel_i_prob = rv.pmf(x)
        mel_text_probs.append(mel_i_prob)
    return torch.tensor(np.array(mel_text_probs))


class Padder(object):
    def __init__(self):
        super(Padder, self).__init__()
        pass

    def _pad1D(self, x, length, pad):
        return np.pad(x, (0, length - x.shape[0]), mode="constant", constant_values=pad)

    def _pad2D(self, x, length, pad):
        return np.pad(
            x, [(0, length - x.shape[0]), (0, 0)], mode="constant", constant_values=pad
        )

    def _pad_durations(self, duration, max_in_len, max_out_len):
        framenum = np.sum(duration)
        symbolnum = duration.shape[0]
        if framenum < max_out_len:
            padframenum = max_out_len - framenum
            duration = np.insert(duration, symbolnum, values=padframenum, axis=0)
            duration = np.insert(
                duration,
                symbolnum + 1,
                values=[0] * (max_in_len - symbolnum - 1),
                axis=0,
            )
        else:
            if symbolnum < max_in_len:
                duration = np.insert(
                    duration, symbolnum, values=[0] * (max_in_len - symbolnum), axis=0
                )
        return duration

    def _round_up(self, x, multiple):
        remainder = x % multiple
        return x if remainder == 0 else x + multiple - remainder

    def _prepare_scalar_inputs(self, inputs, max_len, pad):
        return torch.from_numpy(
            np.stack([self._pad1D(x, max_len, pad) for x in inputs])
        )

    def _prepare_targets(self, targets, max_len, pad):
        return torch.from_numpy(
            np.stack([self._pad2D(t, max_len, pad) for t in targets])
        ).float()

    def _prepare_durations(self, durations, max_in_len, max_out_len):
        return torch.from_numpy(
            np.stack(
                [self._pad_durations(t, max_in_len, max_out_len) for t in durations]
            )
        ).long()


class Voc_Dataset(torch.utils.data.Dataset):
    """
    provide (mel, audio) data pair
    """

    def __init__(
        self,
        metafile,
        root_dir,
        config,
    ):
        self.meta = []
        self.config = config
        self.sampling_rate = config["audio_config"]["sampling_rate"]
        self.n_fft = config["audio_config"]["n_fft"]
        self.hop_length = config["audio_config"]["hop_length"]
        self.batch_max_steps = config["batch_max_steps"]
        self.batch_max_frames = self.batch_max_steps // self.hop_length
        self.aux_context_window = 0  # TODO: make it configurable
        self.start_offset = self.aux_context_window
        self.end_offset = -(self.batch_max_frames + self.aux_context_window)
        self.nsf_enable = (
            config["Model"]["Generator"]["params"].get("nsf_params", None) is not None
        )

        if not isinstance(metafile, list):
            metafile = [metafile]
        if not isinstance(root_dir, list):
            root_dir = [root_dir]

        for meta_file, data_dir in zip(metafile, root_dir):
            if not os.path.exists(meta_file):
                logging.error("meta file not found: {}".format(meta_file))
                raise ValueError(
                    "[Voc_Dataset] meta file: {} not found".format(meta_file)
                )
            if not os.path.exists(data_dir):
                logging.error("data directory not found: {}".format(data_dir))
                raise ValueError(
                    "[Voc_Dataset] data dir: {} not found".format(data_dir)
                )
            self.meta.extend(self.load_meta(meta_file, data_dir))

        #  Load from training data directory
        if len(self.meta) == 0 and isinstance(root_dir, str):
            wav_dir = os.path.join(root_dir, "wav")
            mel_dir = os.path.join(root_dir, "mel")
            if not os.path.exists(wav_dir) or not os.path.exists(mel_dir):
                raise ValueError("wav or mel directory not found")
            self.meta.extend(self.load_meta_from_dir(wav_dir, mel_dir))
        elif len(self.meta) == 0 and isinstance(root_dir, list):
            for d in root_dir:
                wav_dir = os.path.join(d, "wav")
                mel_dir = os.path.join(d, "mel")
                if not os.path.exists(wav_dir) or not os.path.exists(mel_dir):
                    raise ValueError("wav or mel directory not found")
                self.meta.extend(self.load_meta_from_dir(wav_dir, mel_dir))

        self.allow_cache = config["allow_cache"]
        if self.allow_cache:
            self.manager = Manager()
            self.caches = self.manager.list()
            self.caches += [() for _ in range(len(self.meta))]

    @staticmethod
    def gen_metafile(wav_dir, out_dir, split_ratio=0.98):
        wav_files = glob.glob(os.path.join(wav_dir, "*.wav"))
        frame_f0_dir = os.path.join(out_dir, "frame_f0")
        frame_uv_dir = os.path.join(out_dir, "frame_uv")
        mel_dir = os.path.join(out_dir, "mel")
        random.shuffle(wav_files)
        num_train = int(len(wav_files) * split_ratio) - 1
        with open(os.path.join(out_dir, "train.lst"), "w") as f:
            for wav_file in wav_files[:num_train]:
                index = os.path.splitext(os.path.basename(wav_file))[0]
                if (
                    not os.path.exists(os.path.join(frame_f0_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(frame_uv_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(mel_dir, index + ".npy"))
                ):
                    continue
                f.write("{}\n".format(index))

        with open(os.path.join(out_dir, "valid.lst"), "w") as f:
            for wav_file in wav_files[num_train:]:
                index = os.path.splitext(os.path.basename(wav_file))[0]
                if (
                    not os.path.exists(os.path.join(frame_f0_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(frame_uv_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(mel_dir, index + ".npy"))
                ):
                    continue
                f.write("{}\n".format(index))

    def load_meta(self, metafile, data_dir):
        with open(metafile, "r") as f:
            lines = f.readlines()
        wav_dir = os.path.join(data_dir, "wav")
        mel_dir = os.path.join(data_dir, "mel")
        frame_f0_dir = os.path.join(data_dir, "frame_f0")
        frame_uv_dir = os.path.join(data_dir, "frame_uv")
        if not os.path.exists(wav_dir) or not os.path.exists(mel_dir):
            raise ValueError("wav or mel directory not found")
        items = []
        logging.info("Loading metafile...")
        for name in tqdm(lines):
            name = name.strip()
            mel_file = os.path.join(mel_dir, name + ".npy")
            wav_file = os.path.join(wav_dir, name + ".wav")
            frame_f0_file = os.path.join(frame_f0_dir, name + ".npy")
            frame_uv_file = os.path.join(frame_uv_dir, name + ".npy")
            items.append((wav_file, mel_file, frame_f0_file, frame_uv_file))
        return items

    def load_meta_from_dir(self, wav_dir, mel_dir):
        wav_files = glob.glob(os.path.join(wav_dir, "*.wav"))
        items = []
        for wav_file in wav_files:
            mel_file = os.path.join(mel_dir, os.path.basename(wav_file))
            if os.path.exists(mel_file):
                items.append((wav_file, mel_file))
        return items

    def __len__(self):
        return len(self.meta)

    def __getitem__(self, idx):
        if self.allow_cache and len(self.caches[idx]) != 0:
            return self.caches[idx]

        wav_file, mel_file, frame_f0_file, frame_uv_file = self.meta[idx]

        wav_data = librosa.core.load(wav_file, sr=self.sampling_rate)[0]
        mel_data = np.load(mel_file)

        if self.nsf_enable:
            frame_f0_data = np.load(frame_f0_file).reshape(-1, 1)
            frame_uv_data = np.load(frame_uv_file).reshape(-1, 1)
            mel_data = np.concatenate((mel_data, frame_f0_data, frame_uv_data), axis=1)

        # make sure the audio length and feature length are matched
        wav_data = np.pad(wav_data, (0, self.n_fft), mode="reflect")
        wav_data = wav_data[: len(mel_data) * self.hop_length]
        assert len(mel_data) * self.hop_length == len(wav_data)

        if self.allow_cache:
            self.caches[idx] = (wav_data, mel_data)
        return (wav_data, mel_data)

    def collate_fn(self, batch):
        wav_data, mel_data = [item[0] for item in batch], [item[1] for item in batch]
        mel_lengths = [len(mel) for mel in mel_data]

        start_frames = np.array(
            [
                np.random.randint(self.start_offset, length + self.end_offset)
                for length in mel_lengths
            ]
        )

        wav_start = start_frames * self.hop_length
        wav_end = wav_start + self.batch_max_steps

        # aux window works as padding
        mel_start = start_frames - self.aux_context_window
        mel_end = mel_start + self.batch_max_frames + self.aux_context_window

        wav_batch = [
            x[start:end] for x, start, end in zip(wav_data, wav_start, wav_end)
        ]
        mel_batch = [
            c[start:end] for c, start, end in zip(mel_data, mel_start, mel_end)
        ]

        # (B, 1, T)
        wav_batch = torch.tensor(np.asarray(wav_batch), dtype=torch.float32).unsqueeze(
            1
        )
        # (B, C, T)
        mel_batch = torch.tensor(np.asarray(mel_batch), dtype=torch.float32).transpose(
            2, 1
        )
        return wav_batch, mel_batch


def get_voc_datasets(
    config,
    root_dir,
    split_ratio=0.98,
):
    if isinstance(root_dir, str):
        root_dir = [root_dir]
    train_meta_lst = []
    valid_meta_lst = []
    for data_dir in root_dir:
        train_meta = os.path.join(data_dir, "train.lst")
        valid_meta = os.path.join(data_dir, "valid.lst")
        if not os.path.exists(train_meta) or not os.path.exists(valid_meta):
            Voc_Dataset.gen_metafile(
                os.path.join(data_dir, "wav"), data_dir, split_ratio
            )
        train_meta_lst.append(train_meta)
        valid_meta_lst.append(valid_meta)
    train_dataset = Voc_Dataset(
        train_meta_lst,
        root_dir,
        config,
    )

    valid_dataset = Voc_Dataset(
        valid_meta_lst,
        root_dir,
        config,
    )

    return train_dataset, valid_dataset


class AM_Dataset(torch.utils.data.Dataset):
    """
    provide (ling, emo, speaker, mel) pair
    """

    def __init__(
        self,
        config,
        metafile,
        root_dir,
        allow_cache=False,
    ):
        self.meta = []
        self.config = config
        self.with_duration = True
        self.nsf_enable = self.config["Model"]["KanTtsSAMBERT"]["params"].get(
            "NSF", False
        )

        if not isinstance(metafile, list):
            metafile = [metafile]
        if not isinstance(root_dir, list):
            root_dir = [root_dir]

        for meta_file, data_dir in zip(metafile, root_dir):
            if not os.path.exists(meta_file):
                logging.error("meta file not found: {}".format(meta_file))
                raise ValueError(
                    "[AM_Dataset] meta file: {} not found".format(meta_file)
                )
            if not os.path.exists(data_dir):
                logging.error("data dir not found: {}".format(data_dir))
                raise ValueError("[AM_Dataset] data dir: {} not found".format(data_dir))
            self.meta.extend(self.load_meta(meta_file, data_dir))

        self.allow_cache = allow_cache

        self.ling_unit = KanTtsLinguisticUnit(config)
        self.padder = Padder()

        self.r = self.config["Model"]["KanTtsSAMBERT"]["params"]["outputs_per_step"]
        #  TODO: feat window

        if allow_cache:
            self.manager = Manager()
            self.caches = self.manager.list()
            self.caches += [() for _ in range(len(self.meta))]

    def __len__(self):
        return len(self.meta)

    #  TODO: implement __getitem__
    def __getitem__(self, idx):
        if self.allow_cache and len(self.caches[idx]) != 0:
            return self.caches[idx]

        (
            ling_txt,
            mel_file,
            dur_file,
            f0_file,
            energy_file,
            frame_f0_file,
            frame_uv_file,
        ) = self.meta[idx]

        ling_data = self.ling_unit.encode_symbol_sequence(ling_txt)
        mel_data = np.load(mel_file)
        dur_data = np.load(dur_file) if dur_file is not None else None
        f0_data = np.load(f0_file)
        energy_data = np.load(energy_file)
        if self.with_duration:
            attn_prior = None
        else:
            attn_prior = beta_binomial_prior_distribution(
                len(ling_data[0]), mel_data.shape[0]
            )

        # Concat frame-level f0 and uv to mel_data
        if self.nsf_enable:
            frame_f0_data = np.load(frame_f0_file).reshape(-1, 1)
            frame_uv_data = np.load(frame_uv_file).reshape(-1, 1)
            mel_data = np.concatenate([mel_data, frame_f0_data, frame_uv_data], axis=1)

        if self.allow_cache:
            self.caches[idx] = (
                ling_data,
                mel_data,
                dur_data,
                f0_data,
                energy_data,
                attn_prior,
            )

        return (ling_data, mel_data, dur_data, f0_data, energy_data, attn_prior)

    def load_meta(self, metafile, data_dir):
        with open(metafile, "r") as f:
            lines = f.readlines()

        mel_dir = os.path.join(data_dir, "mel")
        dur_dir = os.path.join(data_dir, "duration")
        f0_dir = os.path.join(data_dir, "f0")
        energy_dir = os.path.join(data_dir, "energy")
        frame_f0_dir = os.path.join(data_dir, "frame_f0")
        frame_uv_dir = os.path.join(data_dir, "frame_uv")

        self.with_duration = os.path.exists(dur_dir)

        items = []
        logging.info("Loading metafile...")
        for line in tqdm(lines):
            line = line.strip()
            index, ling_txt = line.split("\t")
            mel_file = os.path.join(mel_dir, index + ".npy")
            if self.with_duration:
                dur_file = os.path.join(dur_dir, index + ".npy")
            else:
                dur_file = None
            f0_file = os.path.join(f0_dir, index + ".npy")
            energy_file = os.path.join(energy_dir, index + ".npy")
            frame_f0_file = os.path.join(frame_f0_dir, index + ".npy")
            frame_uv_file = os.path.join(frame_uv_dir, index + ".npy")

            items.append(
                (
                    ling_txt,
                    mel_file,
                    dur_file,
                    f0_file,
                    energy_file,
                    frame_f0_file,
                    frame_uv_file,
                )
            )

        return items

    @staticmethod
    def gen_metafile(raw_meta_file, out_dir, badlist=None, split_ratio=0.98):
        with open(raw_meta_file, "r") as f:
            lines = f.readlines()
        frame_f0_dir = os.path.join(out_dir, "frame_f0")
        frame_uv_dir = os.path.join(out_dir, "frame_uv")
        mel_dir = os.path.join(out_dir, "mel")
        duration_dir = os.path.join(out_dir, "duration")
        random.shuffle(lines)
        num_train = int(len(lines) * split_ratio) - 1
        with open(os.path.join(out_dir, "am_train.lst"), "w") as f:
            for line in lines[:num_train]:
                index = line.split("\t")[0]
                if badlist is not None and index in badlist:
                    continue
                if (
                    not os.path.exists(os.path.join(frame_f0_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(frame_uv_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(duration_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(mel_dir, index + ".npy"))
                ):
                    continue
                f.write(line)

        with open(os.path.join(out_dir, "am_valid.lst"), "w") as f:
            for line in lines[num_train:]:
                index = line.split("\t")[0]
                if badlist is not None and index in badlist:
                    continue
                if (
                    not os.path.exists(os.path.join(frame_f0_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(frame_uv_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(duration_dir, index + ".npy"))
                    or not os.path.exists(os.path.join(mel_dir, index + ".npy"))
                ):
                    continue
                f.write(line)

    #  TODO: implement collate_fn
    def collate_fn(self, batch):
        data_dict = {}

        max_input_length = max((len(x[0][0]) for x in batch))

        # pure linguistic info: sy|tone|syllable_flag|word_segment
        lfeat_type = self.ling_unit._lfeat_type_list[0]
        inputs_sy = self.padder._prepare_scalar_inputs(
            [x[0][0] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()
        # tone
        lfeat_type = self.ling_unit._lfeat_type_list[1]
        inputs_tone = self.padder._prepare_scalar_inputs(
            [x[0][1] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()

        # syllable_flag
        lfeat_type = self.ling_unit._lfeat_type_list[2]
        inputs_syllable_flag = self.padder._prepare_scalar_inputs(
            [x[0][2] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()

        # word_segment
        lfeat_type = self.ling_unit._lfeat_type_list[3]
        inputs_ws = self.padder._prepare_scalar_inputs(
            [x[0][3] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()

        # emotion category
        lfeat_type = self.ling_unit._lfeat_type_list[4]
        data_dict["input_emotions"] = self.padder._prepare_scalar_inputs(
            [x[0][4] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()

        # speaker category
        lfeat_type = self.ling_unit._lfeat_type_list[5]
        data_dict["input_speakers"] = self.padder._prepare_scalar_inputs(
            [x[0][5] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()

        data_dict["input_lings"] = torch.stack(
            [inputs_sy, inputs_tone, inputs_syllable_flag, inputs_ws], dim=2
        )
        data_dict["valid_input_lengths"] = torch.as_tensor(
            [len(x[0][0]) - 1 for x in batch], dtype=torch.long
        )  # 输入的symbol sequence会在后面拼一个“~”，影响duration计算，所以把length-1
        data_dict["valid_output_lengths"] = torch.as_tensor(
            [len(x[1]) for x in batch], dtype=torch.long
        )

        max_output_length = torch.max(data_dict["valid_output_lengths"]).item()
        max_output_round_length = self.padder._round_up(max_output_length, self.r)

        data_dict["mel_targets"] = self.padder._prepare_targets(
            [x[1] for x in batch], max_output_round_length, 0.0
        )
        if self.with_duration:
            data_dict["durations"] = self.padder._prepare_durations(
                [x[2] for x in batch], max_input_length, max_output_round_length
            )
        else:
            data_dict["durations"] = None

        if self.with_duration:
            feats_padding_length = max_input_length
        else:
            feats_padding_length = max_output_round_length

        data_dict["pitch_contours"] = self.padder._prepare_scalar_inputs(
            [x[3] for x in batch], feats_padding_length, 0.0
        ).float()
        data_dict["energy_contours"] = self.padder._prepare_scalar_inputs(
            [x[4] for x in batch], feats_padding_length, 0.0
        ).float()

        if self.with_duration:
            data_dict["attn_priors"] = None
        else:
            data_dict["attn_priors"] = torch.zeros(
                len(batch), max_output_round_length, max_input_length
            )
            for i in range(len(batch)):
                attn_prior = batch[i][5]
                data_dict["attn_priors"][
                    i, : attn_prior.shape[0], : attn_prior.shape[1]
                ] = attn_prior

        return data_dict


#  TODO: implement get_am_datasets
def get_am_datasets(
    metafile,
    root_dir,
    config,
    allow_cache,
    split_ratio=0.98,
):
    if not isinstance(root_dir, list):
        root_dir = [root_dir]
    if not isinstance(metafile, list):
        metafile = [metafile]

    train_meta_lst = []
    valid_meta_lst = []

    for raw_metafile, data_dir in zip(metafile, root_dir):
        train_meta = os.path.join(data_dir, "am_train.lst")
        valid_meta = os.path.join(data_dir, "am_valid.lst")
        if not os.path.exists(train_meta) or not os.path.exists(valid_meta):
            AM_Dataset.gen_metafile(raw_metafile, data_dir, split_ratio)
        train_meta_lst.append(train_meta)
        valid_meta_lst.append(valid_meta)

    train_dataset = AM_Dataset(config, train_meta_lst, root_dir, allow_cache)

    valid_dataset = AM_Dataset(config, valid_meta_lst, root_dir, allow_cache)

    return train_dataset, valid_dataset


class MaskingActor(object):
    def __init__(self, mask_ratio=0.15):
        super(MaskingActor, self).__init__()
        self.mask_ratio = mask_ratio
        pass

    def _get_random_mask(self, length, p1=0.15):
        mask = np.random.uniform(0, 1, length)
        index = 0
        while index < len(mask):
            if mask[index] < p1:
                mask[index] = 1
            else:
                mask[index] = 0
            index += 1

        return mask

    def _input_bert_masking(
        self,
        sequence_array,
        nb_symbol_category,
        mask_symbol_id,
        mask,
        p2=0.8,
        p3=0.1,
        p4=0.1,
    ):
        sequence_array_mask = sequence_array.copy()
        mask_id = np.where(mask == 1)[0]
        mask_len = len(mask_id)
        rand = np.arange(mask_len)
        np.random.shuffle(rand)

        # [MASK]
        mask_id_p2 = mask_id[rand[0 : int(math.floor(mask_len * p2))]]
        if len(mask_id_p2) > 0:
            sequence_array_mask[mask_id_p2] = mask_symbol_id

        # rand
        mask_id_p3 = mask_id[
            rand[
                int(math.floor(mask_len * p2)) : int(math.floor(mask_len * p2))
                + int(math.floor(mask_len * p3))
            ]
        ]
        if len(mask_id_p3) > 0:
            sequence_array_mask[mask_id_p3] = random.randint(0, nb_symbol_category - 1)

        # ori
        # do nothing

        return sequence_array_mask


class BERT_Text_Dataset(torch.utils.data.Dataset):
    """
    provide (ling, ling_sy_masked, bert_mask) pair
    """

    def __init__(
        self,
        config,
        metafile,
        root_dir,
        allow_cache=False,
    ):
        self.meta = []
        self.config = config

        if not isinstance(metafile, list):
            metafile = [metafile]
        if not isinstance(root_dir, list):
            root_dir = [root_dir]

        for meta_file, data_dir in zip(metafile, root_dir):
            if not os.path.exists(meta_file):
                logging.error("meta file not found: {}".format(meta_file))
                raise ValueError(
                    "[BERT_Text_Dataset] meta file: {} not found".format(meta_file)
                )
            if not os.path.exists(data_dir):
                logging.error("data dir not found: {}".format(data_dir))
                raise ValueError(
                    "[BERT_Text_Dataset] data dir: {} not found".format(data_dir)
                )
            self.meta.extend(self.load_meta(meta_file, data_dir))

        self.allow_cache = allow_cache

        self.ling_unit = KanTtsLinguisticUnit(config)
        self.padder = Padder()
        self.masking_actor = MaskingActor(
            self.config["Model"]["KanTtsTextsyBERT"]["params"]["mask_ratio"]
        )

        if allow_cache:
            self.manager = Manager()
            self.caches = self.manager.list()
            self.caches += [() for _ in range(len(self.meta))]

    def __len__(self):
        return len(self.meta)

    #  TODO: implement __getitem__
    def __getitem__(self, idx):
        if self.allow_cache and len(self.caches[idx]) != 0:
            ling_data = self.caches[idx][0]
            bert_mask, ling_sy_masked_data = self.bert_masking(ling_data)
            return (ling_data, ling_sy_masked_data, bert_mask)

        ling_txt = self.meta[idx]

        ling_data = self.ling_unit.encode_symbol_sequence(ling_txt)
        bert_mask, ling_sy_masked_data = self.bert_masking(ling_data)

        if self.allow_cache:
            self.caches[idx] = (ling_data,)

        return (ling_data, ling_sy_masked_data, bert_mask)

    def load_meta(self, metafile, data_dir):
        with open(metafile, "r") as f:
            lines = f.readlines()

        items = []
        logging.info("Loading metafile...")
        for line in tqdm(lines):
            line = line.strip()
            index, ling_txt = line.split("\t")

            items.append((ling_txt))

        return items

    @staticmethod
    def gen_metafile(raw_meta_file, out_dir, split_ratio=0.98):
        with open(raw_meta_file, "r") as f:
            lines = f.readlines()
        random.shuffle(lines)
        num_train = int(len(lines) * split_ratio) - 1
        with open(os.path.join(out_dir, "bert_train.lst"), "w") as f:
            for line in lines[:num_train]:
                f.write(line)

        with open(os.path.join(out_dir, "bert_valid.lst"), "w") as f:
            for line in lines[num_train:]:
                f.write(line)

    def bert_masking(self, ling_data):
        length = len(ling_data[0])
        mask = self.masking_actor._get_random_mask(
            length, p1=self.masking_actor.mask_ratio
        )
        mask[-1] = 0

        # sy_masked
        sy_mask_symbol_id = self.ling_unit.encode_sy([self.ling_unit._mask])[0]
        ling_sy_masked_data = self.masking_actor._input_bert_masking(
            ling_data[0],
            self.ling_unit.get_unit_size()["sy"],
            sy_mask_symbol_id,
            mask,
            p2=0.8,
            p3=0.1,
            p4=0.1,
        )

        return (mask, ling_sy_masked_data)

    #  TODO: implement collate_fn
    def collate_fn(self, batch):
        data_dict = {}

        max_input_length = max((len(x[0][0]) for x in batch))

        # pure linguistic info: sy|tone|syllable_flag|word_segment
        # sy
        lfeat_type = self.ling_unit._lfeat_type_list[0]
        targets_sy = self.padder._prepare_scalar_inputs(
            [x[0][0] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()
        # sy masked
        inputs_sy = self.padder._prepare_scalar_inputs(
            [x[1] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()
        # tone
        lfeat_type = self.ling_unit._lfeat_type_list[1]
        inputs_tone = self.padder._prepare_scalar_inputs(
            [x[0][1] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()

        # syllable_flag
        lfeat_type = self.ling_unit._lfeat_type_list[2]
        inputs_syllable_flag = self.padder._prepare_scalar_inputs(
            [x[0][2] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()

        # word_segment
        lfeat_type = self.ling_unit._lfeat_type_list[3]
        inputs_ws = self.padder._prepare_scalar_inputs(
            [x[0][3] for x in batch],
            max_input_length,
            self.ling_unit._sub_unit_pad[lfeat_type],
        ).long()

        data_dict["input_lings"] = torch.stack(
            [inputs_sy, inputs_tone, inputs_syllable_flag, inputs_ws], dim=2
        )
        data_dict["valid_input_lengths"] = torch.as_tensor(
            [len(x[0][0]) - 1 for x in batch], dtype=torch.long
        )  # 输入的symbol sequence会在后面拼一个“~”，影响duration计算，所以把length-1

        data_dict["targets"] = targets_sy
        data_dict["bert_masks"] = self.padder._prepare_scalar_inputs(
            [x[2] for x in batch], max_input_length, 0.0
        )

        return data_dict


def get_bert_text_datasets(
    metafile,
    root_dir,
    config,
    allow_cache,
    split_ratio=0.98,
):
    if not isinstance(root_dir, list):
        root_dir = [root_dir]
    if not isinstance(metafile, list):
        metafile = [metafile]

    train_meta_lst = []
    valid_meta_lst = []

    for raw_metafile, data_dir in zip(metafile, root_dir):
        train_meta = os.path.join(data_dir, "bert_train.lst")
        valid_meta = os.path.join(data_dir, "bert_valid.lst")
        if not os.path.exists(train_meta) or not os.path.exists(valid_meta):
            BERT_Text_Dataset.gen_metafile(raw_metafile, data_dir, split_ratio)
        train_meta_lst.append(train_meta)
        valid_meta_lst.append(valid_meta)

    train_dataset = BERT_Text_Dataset(config, train_meta_lst, root_dir, allow_cache)

    valid_dataset = BERT_Text_Dataset(config, valid_meta_lst, root_dir, allow_cache)

    return train_dataset, valid_dataset
