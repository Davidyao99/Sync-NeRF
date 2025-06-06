import logging as log
import math
import os
from collections import defaultdict
from typing import Dict, MutableMapping, Union, Any, List
from typing import Optional, Tuple
import numpy as np

import pandas as pd
import torch
import torch.utils.data

from plenoxels.datasets.video_datasets import Video360Dataset
from plenoxels.utils.ema import EMA
from plenoxels.utils.my_tqdm import tqdm
from plenoxels.ops.image import metrics
from plenoxels.ops.image.io import write_video_to_file
from plenoxels.models.lowrank_model import LowrankModel
from .base_trainer import BaseTrainer, init_dloader_random, initialize_model
from .regularization import (
    PlaneTV, TimeSmoothness, HistogramLoss, L1TimePlanes, DistortionLoss,Density_L1
)


class VideoTrainer(BaseTrainer):
    def __init__(self,
                 tr_loader: torch.utils.data.DataLoader,
                 tr_dset: torch.utils.data.TensorDataset,
                 ts_dset: torch.utils.data.TensorDataset,
                 num_steps: int,
                 logdir: str,
                 expname: str,
                 train_fp16: bool,
                 save_every: int,
                 valid_every: int,
                 save_outputs: bool,
                 isg_step: int,
                 ist_step: int,
                 device: Union[str, torch.device],
                 **kwargs
                 ):
        self.train_dataset = tr_dset
        self.test_dataset = ts_dset
        self.ist_step = ist_step
        self.isg_step = isg_step
        self.save_video = save_outputs
        # Switch to compute extra video metrics (FLIP, JOD)
        self.compute_video_metrics = False
        super().__init__(
            train_data_loader=tr_loader,
            num_steps=num_steps,
            logdir=logdir,
            expname=expname,
            train_fp16=train_fp16,
            save_every=save_every,
            valid_every=valid_every,
            save_outputs=False,  # False since we're saving video
            device=device,
            **kwargs)

    def eval_step(self, data, **kwargs) -> MutableMapping[str, torch.Tensor]:
        """
        Note that here `data` contains a whole image. we need to split it up before tracing
        for memory constraints.
        """
        super().eval_step(data, **kwargs)
        batch_size = self.eval_batch_size
        with torch.cuda.amp.autocast(enabled=self.train_fp16), torch.no_grad():
            rays_o = data["rays_o"]
            rays_d = data["rays_d"]
            timestamp = data["timestamps"]
            near_far = data["near_fars"].to(self.device)
            bg_color = data["bg_color"]
            if isinstance(bg_color, torch.Tensor):
                bg_color = bg_color.to(self.device)
            preds = defaultdict(list)
            for b in range(math.ceil(rays_o.shape[0] / batch_size)):
                rays_o_b = rays_o[b * batch_size: (b + 1) * batch_size].to(self.device)
                rays_d_b = rays_d[b * batch_size: (b + 1) * batch_size].to(self.device)
                timestamps_d_b = timestamp.expand(rays_o_b.shape[0]).to(self.device)
                outputs = self.model(
                    rays_o_b, rays_d_b, timestamps=timestamps_d_b, bg_color=bg_color, 
                    near_far=near_far, camids=data['camids'])
                for k, v in outputs.items():
                    if "rgb" in k or "depth" in k:
                        preds[k].append(v.cpu())
        return {k: torch.cat(v, 0) for k, v in preds.items()}

    def train_step(self, data: Dict[str, Union[int, torch.Tensor]], **kwargs):
        scale_ok = super().train_step(data, **kwargs)

        if self.global_step == self.isg_step:
            self.train_dataset.enable_isg()
            raise StopIteration  # Whenever we change the dataset
        if self.global_step == self.ist_step:
            self.train_dataset.switch_isg2ist()
            raise StopIteration  # Whenever we change the dataset

        return scale_ok

    def post_step(self, progress_bar):
        super().post_step(progress_bar)

    def pre_epoch(self):
        super().pre_epoch()
        # Reset randomness in train-dataset
        self.train_dataset.reset_iter()

    def evaluate_metrics_simple(self,
                         gt: Optional[torch.Tensor],
                         preds: MutableMapping[str, torch.Tensor],
                         dset,
                         img_idx: int,
                         name: Optional[str] = None,
                         save_outputs: bool = True) -> Tuple[dict, np.ndarray, Optional[np.ndarray]]:
        if isinstance(dset.img_h, int):
            img_h, img_w = dset.img_h, dset.img_w
        else:
            img_h, img_w = dset.img_h[img_idx], dset.img_w[img_idx]
        preds_rgb = (
            preds["rgb"]
            .reshape(img_h, img_w, 3)
            .cpu()
            .clamp(0, 1)
        )
        if not torch.isfinite(preds_rgb).all():
            log.warning(f"Predictions have {torch.isnan(preds_rgb).sum()} NaNs, "
                        f"{torch.isinf(preds_rgb).sum()} infs.")
            preds_rgb = torch.nan_to_num(preds_rgb, nan=0.0)
        out_img_only = preds_rgb
        summary = dict()

        if gt is not None:
            gt = gt.reshape(img_h, img_w, -1).cpu()
            if gt.shape[-1] == 4:
                gt = gt[..., :3] * gt[..., 3:] + (1.0 - gt[..., 3:])
            summary.update(self.calc_metrics(preds_rgb, gt))

        out_depth_np: Optional[np.ndarray] = None
        out_img_np: Optional[np.ndarray] = None
        out_img_only_np: np.ndarray = (out_img_only * 255.0).byte().numpy()

        return summary, out_img_np, out_depth_np, out_img_only_np


    @torch.no_grad()
    def validate(self):
        dataset = self.test_dataset
        per_scene_metrics: Dict[str, Union[float, List]] = defaultdict(list)
        pred_frames, out_depths, out_imgs_only = [], [], []
        pb = tqdm(total=len(dataset), desc=f"Test scene ({dataset.name})")
        for img_idx, data in enumerate(dataset):
            preds = self.eval_step(data)
            out_metrics, out_img, out_depth, out_img_only = self.evaluate_metrics(
                data["imgs"], preds, dset=dataset, img_idx=img_idx, name=None,
                save_outputs=self.save_outputs)
            pred_frames.append(out_img)
            out_imgs_only.append(out_img_only)
            if out_depth is not None:
                out_depths.append(out_depth)
            for k, v in out_metrics.items():
                per_scene_metrics[k].append(v)
            pb.set_postfix_str(f"PSNR={out_metrics['psnr']:.2f}", refresh=False)
            pb.update(1)
        pb.close()
        if self.save_video:
            write_video_to_file(
                os.path.join(self.log_dir, f"step{self.global_step}.mp4"),
                pred_frames
            )
            write_video_to_file(
                os.path.join(self.log_dir, f"step{self.global_step}-pred.mp4"),
                out_imgs_only
            )
            if len(out_depths) > 0:
                write_video_to_file(
                    os.path.join(self.log_dir, f"step{self.global_step}-depth.mp4"),
                    out_depths
                )
        # Calculate JOD (on whole video)
        if self.compute_video_metrics:
            per_scene_metrics["JOD"] = metrics.jod(
                [f[:dataset.img_h, :, :] for f in pred_frames],
                [f[dataset.img_h: 2*dataset.img_h, :, :] for f in pred_frames],
            )
            per_scene_metrics["FLIP"] = metrics.flip(
                [f[:dataset.img_h, :, :] for f in pred_frames],
                [f[dataset.img_h: 2*dataset.img_h, :, :] for f in pred_frames],
            )

        val_metrics = [
            self.report_test_metrics(per_scene_metrics, extra_name=None),
        ]
        df = pd.DataFrame.from_records(val_metrics)
        df.to_csv(os.path.join(self.log_dir, f"test_metrics_step{self.global_step}.csv"))


    @torch.no_grad()
    def validate_simple(self):
        dataset = self.test_dataset
        per_scene_metrics: Dict[str, Union[float, List]] = defaultdict(list)
        pred_frames, out_depths, out_imgs_only = [], [], []
        pb = tqdm(total=len(dataset), desc=f"Test scene ({dataset.name})")
        for img_idx, data in enumerate(dataset):
            preds = self.eval_step(data)
            out_metrics, out_img, out_depth, out_img_only = self.evaluate_metrics_simple(
                data["imgs"], preds, dset=dataset, img_idx=img_idx, name=None,
                save_outputs=self.save_outputs)
            out_imgs_only.append(out_img_only)
            for k, v in out_metrics.items():
                per_scene_metrics[k].append(v)
            pb.set_postfix_str(f"PSNR={out_metrics['psnr']:.2f}", refresh=False)
            pb.update(1)
        pb.close()
        if self.save_video:
            write_video_to_file(
                os.path.join(self.log_dir, f"step{self.global_step}-pred.mp4"),
                out_imgs_only
            )

        val_metrics = [
            self.report_test_metrics(per_scene_metrics, extra_name=None),
        ]
        df = pd.DataFrame.from_records(val_metrics)
        df.to_csv(os.path.join(self.log_dir, f"test_metrics_step{self.global_step}.csv"))
        return per_scene_metrics

    def get_save_dict(self):
        base_save_dict = super().get_save_dict()
        return base_save_dict

    def load_model(self, checkpoint_data, training_needed: bool = True):
        super().load_model(checkpoint_data, training_needed)
        if self.train_dataset is not None:
            if -1 < self.isg_step < self.global_step < self.ist_step:
                self.train_dataset.enable_isg()
            elif -1 < self.ist_step < self.global_step:
                self.train_dataset.switch_isg2ist()

    def init_epoch_info(self):
        ema_weight = 0.9
        loss_info = defaultdict(lambda: EMA(ema_weight))
        return loss_info

    def init_model(self, **kwargs) -> LowrankModel:
        return initialize_model(self, **kwargs)

    def get_regularizers(self, **kwargs):
        return [
            PlaneTV(kwargs.get('plane_tv_weight', 0.0), what='field'),
            PlaneTV(kwargs.get('plane_tv_weight_proposal_net', 0.0), what='proposal_network'),
            L1TimePlanes(kwargs.get('l1_time_planes', 0.0), what='field'),
            L1TimePlanes(kwargs.get('l1_time_planes_proposal_net', 0.0), what='proposal_network'),
            TimeSmoothness(kwargs.get('time_smoothness_weight', 0.0), what='field'),
            TimeSmoothness(kwargs.get('time_smoothness_weight_proposal_net', 0.0), what='proposal_network'),
            HistogramLoss(kwargs.get('histogram_loss_weight', 0.0)),
            DistortionLoss(kwargs.get('distortion_loss_weight', 0.0)),
            Density_L1(kwargs.get('density_l1', 0.0)),
        ]

    @property
    def calc_metrics_every(self):
        return 5


def init_tr_data(data_downsample, data_dir, split, **kwargs):
    isg = kwargs.get('isg', False)
    ist = kwargs.get('ist', False)
    keyframes = kwargs.get('keyframes', False)
    batch_size = kwargs['batch_size']
    log.info(f"Loading Video360Dataset with downsample={data_downsample}")
    tr_dset = Video360Dataset(
        data_dir, split=split, downsample=data_downsample,
        batch_size=batch_size,
        max_cameras=kwargs.get('max_train_cameras', None),
        max_tsteps=kwargs['max_train_tsteps'] if keyframes else None,
        isg=isg, keyframes=keyframes, contraction=kwargs['contract'], ndc=kwargs['ndc'],
        near_scaling=float(kwargs.get('near_scaling', 0)), ndc_far=float(kwargs.get('ndc_far', 0)),
        scene_bbox=kwargs['scene_bbox'],
        normalize_scale = kwargs.get('normalize_scale', False),
        num_frames = kwargs.get('num_frames', False),
        video = kwargs.get('video', False),
    )
    if ist:
        tr_dset.switch_isg2ist()  # this should only happen in case we're reloading

    g = torch.Generator()
    g.manual_seed(0)
    tr_loader = torch.utils.data.DataLoader(
        # tr_dset, batch_size=None, num_workers=3,  prefetch_factor=3, pin_memory=True,
        tr_dset, batch_size=None, num_workers=0, pin_memory=True,
        worker_init_fn=init_dloader_random, generator=g)
    return {"tr_loader": tr_loader, "tr_dset": tr_dset}

def init_ts_data(data_downsample,data_dir, split, hold_id=0, **kwargs):
    ts_dset = Video360Dataset(
        data_dir, split=split, downsample=data_downsample,
        max_cameras=kwargs.get('max_test_cameras', None), max_tsteps=kwargs.get('max_test_tsteps', None),
        contraction=kwargs['contract'], ndc=kwargs['ndc'],
        near_scaling=float(kwargs.get('near_scaling', 0)), ndc_far=float(kwargs.get('ndc_far', 0)),
        hold_id=hold_id,
        scene_bbox=kwargs['scene_bbox'],
        normalize_scale = kwargs.get('normalize_scale', False),
        num_frames = kwargs.get('num_frames', False),
        video = kwargs.get('video', False),
    )
    return {"ts_dset": ts_dset}


def load_data(data_downsample, data_dirs, validate_only, render_only, test_optim, hold_id=0, **kwargs):
    assert len(data_dirs) == 1
    od: Dict[str, Any] = {}
    if not validate_only and not render_only:
        split = "test_optim" if test_optim else "train"
        od.update(init_tr_data(data_downsample, data_dirs[0], split=split, **kwargs))
    else:
        od.update(tr_loader=None, tr_dset=None)
    test_split = "render" if render_only else "test"
    od.update(init_ts_data(data_downsample,data_dirs[0], split=test_split, hold_id=hold_id, **kwargs))
    return od
