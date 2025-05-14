import glob
import json
import logging as log
import math
import os
import time
from collections import defaultdict
from typing import Optional, List, Tuple, Any, Dict

import numpy as np
import torch

from .base_dataset import BaseDataset
from .data_loading import parallel_load_images
from .intrinsics import Intrinsics
from .llff_dataset import load_llff_poses_helper
from .ray_utils import (
    generate_spherical_poses, create_meshgrid, stack_camera_dirs, get_rays, generate_spiral_path
)
from .synthetic_nerf_dataset import (
    load_360_images, load_360_intrinsics,
)


class Video360Dataset(BaseDataset):
    len_time: int
    max_cameras: Optional[int]
    max_tsteps: Optional[int]
    timestamps: Optional[torch.Tensor]

    def __init__(self,
                 datadir: str,
                 split: str,
                 batch_size: Optional[int] = None,
                 downsample: float = 1.0,
                 keyframes: bool = False,
                 max_cameras: Optional[int] = None,
                 max_tsteps: Optional[int] = None,
                 isg: bool = False,
                 contraction: bool = False,
                 ndc: bool = False,
                 scene_bbox: Optional[List] = None,
                 near_scaling: float = 0.9,
                 hold_id = 0,
                 ndc_far: float = 2.6,
                 normalize_scale: float = 0.8,
                 num_frames: int = 300,
                 video: Optional[str] = None,):
        self.keyframes = keyframes
        self.max_cameras = max_cameras
        self.max_tsteps = max_tsteps
        self.downsample = downsample
        self.isg = isg
        self.ist = False
        # self.lookup_time = False
        self.per_cam_near_fars = None
        self.global_translation = torch.tensor([0, 0, 0])
        self.global_scale = torch.tensor([1, 1, 1])
        self.near_scaling = near_scaling
        self.ndc_far = ndc_far
        self.hold_id = hold_id
        self.median_imgs = None
        self.normalize_scale = normalize_scale
        self.num_frames = num_frames
        self.video = video
        if contraction and ndc:
            raise ValueError("Options 'contraction' and 'ndc' are exclusive.")
        if "lego" in datadir or "dnerf" in datadir:
            dset_type = "synthetic"
        elif "fox" in datadir or "box" in datadir or "deer" in datadir or "blender" in datadir or "data_preprocessed" in datadir:
            dset_type = "blender"
        else:
            dset_type = "llff"

        # Note: timestamps are stored normalized between -1, 1.
        if dset_type == "llff":
            if split == "render":
                assert ndc, "Unable to generate render poses without ndc: don't know near-far."
                per_cam_poses, per_cam_near_fars, intrinsics, _ = load_llffvideo_poses(
                    datadir, downsample=self.downsample, split='all', img_sizes=img_sizes, near_scaling=self.near_scaling)
                render_poses = generate_spiral_path(
                    per_cam_poses.numpy(), per_cam_near_fars.numpy(), n_frames=300,
                    n_rots=2, zrate=0.5, dt=self.near_scaling, percentile=60)
                self.poses = torch.from_numpy(render_poses).float()
                self.per_cam_near_fars = torch.tensor([[0.4, self.ndc_far]])
                timestamps = torch.linspace(0, (self.num_frames-1), len(self.poses))
                imgs = None
            else:
                if split == 'test_optim':
                    per_cam_poses, per_cam_near_fars, intrinsics, videopaths, cam_nums = load_llffvideo_poses_testoptim(
                    datadir, downsample=self.downsample, split=split, near_scaling=self.near_scaling, hold_id=hold_id)

                else:
                    per_cam_poses, per_cam_near_fars, intrinsics, videopaths, cam_nums = load_llffvideo_poses(
                    datadir, downsample=self.downsample, split=split, near_scaling=self.near_scaling, hold_id=hold_id)

                    if split == 'test':
                        keyframes = False

                poses, imgs, timestamps, self.median_imgs = load_llffvideo_data(
                    videopaths=videopaths, cam_poses=per_cam_poses, intrinsics=intrinsics,
                    split=split, keyframes=keyframes, keyframes_take_each=30)
                self.poses = poses.float()
                if contraction:
                    self.per_cam_near_fars = per_cam_near_fars.float()
                else:
                    self.per_cam_near_fars = torch.tensor(
                        [[0.0, self.ndc_far]]).repeat(per_cam_near_fars.shape[0], 1)
                self.cam_nums = cam_nums 

            # These values are tuned for the salmon video
            self.global_translation = torch.tensor([0, 0, 2.])
            self.global_scale = torch.tensor([0.5, 0.6, 1])
            # Normalize timestamps between -1, 1
            timestamps = (timestamps.float() / (self.num_frames-1)) * 2 - 1

        elif dset_type == "synthetic":
            assert not contraction, "Synthetic video dataset does not work with contraction."
            assert not ndc, "Synthetic video dataset does not work with NDC."
            if split == 'render':
                num_tsteps = 120
                dnerf_durations = {'hellwarrior': 100, 'mutant': 150, 'hook': 100, 'bouncingballs': 150, 'lego': 50, 'trex': 200, 'standup': 150, 'jumpingjacks': 200}
                for scene in dnerf_durations.keys():
                    if 'dnerf' in datadir and scene in datadir:
                        num_tsteps = dnerf_durations[scene]
                render_poses = torch.stack([
                    generate_spherical_poses(angle, -30.0, 4.0)
                    for angle in np.linspace(-180, 180, num_tsteps + 1)[:-1]
                ], 0)
                imgs = None
                self.poses = render_poses
                timestamps = torch.linspace(0.0, 1.0, render_poses.shape[0])
                _, transform = load_360video_frames(
                    datadir, 'train', max_cameras=self.max_cameras, max_tsteps=self.max_tsteps)
                img_h, img_w = 800, 800
            else:
                frames, transform = load_360video_frames(
                    datadir, split, max_cameras=self.max_cameras, max_tsteps=self.max_tsteps)
                imgs, self.poses = load_360_images(frames, datadir, split, self.downsample)
                timestamps = torch.tensor(
                    [fetch_360vid_info(f)[0] for f in frames], dtype=torch.float32)
                img_h, img_w = imgs[0].shape[:2]
                
            if ndc:
                self.per_cam_near_fars = torch.tensor([[0.0, self.ndc_far]])
            else:
                self.per_cam_near_fars = torch.tensor([[2.0, 6.0]])
            if "dnerf" in datadir:
                # dnerf time is between 0, 1. Normalize to -1, 1
                timestamps = timestamps * 2 - 1
            else:
                # lego (our vid) time is like dynerf: between 0, 30.
                timestamps = (timestamps.float() / torch.amax(timestamps)) * 2 - 1
            intrinsics = load_360_intrinsics(
                transform, img_h=img_h, img_w=img_w, downsample=self.downsample)
        elif dset_type == "blender":
            if split == "render":
                # assert ndc, "Unable to generate render poses without ndc: don't know near-far."
                # per_cam_poses, per_cam_near_fars, intrinsics, _ = load_llffvideo_poses(
                #     datadir, downsample=self.downsample, split='all', near_scaling=self.near_scaling)
                
                per_cam_poses, intrinsics, videopaths, cam_nums, img_sizes = load_blendervideo_poses_custom(
                        datadir, downsample=self.downsample, split=split,hold_id=hold_id, near_scaling=self.near_scaling, video=self.video)
                self.img_sizes = img_sizes
                self.per_cam_near_fars = torch.tensor(
                        # [[2.0, 6.0]]).repeat(per_cam_poses.shape[0], 1)
                        [[0.1, 6.0]]).repeat(per_cam_poses.shape[0], 1)
                per_cam_poses = per_cam_poses[:, :3]
                render_poses = generate_spiral_path(
                    per_cam_poses.numpy(), self.per_cam_near_fars.numpy(), n_frames=300,
                    n_rots=2, zrate=0.5, dt=self.near_scaling, percentile=60)
                self.poses = torch.from_numpy(render_poses).float()
                # self.per_cam_near_fars = torch.tensor([[0.4, self.ndc_far]])
                self.cam_nums = cam_nums
                self.per_cam_near_fars = torch.tensor(
                        # [[2.0, 6.0]]).repeat(per_cam_poses.shape[0], 1)
                        [[0.1, 6.0]]).repeat(1, 1)
                timestamps = torch.linspace(0, 299, len(self.poses))
                imgs = None
            else:
                if split == 'test_optim':
                    per_cam_poses, intrinsics, videopaths, cam_nums = load_blendervideo_poses_testoptim(
                        datadir, downsample=self.downsample, split=split,hold_id=hold_id, near_scaling=self.near_scaling)
                else:
                    per_cam_poses, intrinsics, videopaths, cam_nums, img_sizes = load_blendervideo_poses_custom(
                        datadir, downsample=self.downsample, split=split,hold_id=hold_id, near_scaling=self.near_scaling, video=self.video)
                    # breakpoint()
                    # per_cam_poses, intrinsics, videopaths, cam_nums = load_blendervideo_poses(
                    #     datadir, downsample=self.downsample, split=split,hold_id=hold_id, near_scaling=self.near_scaling)

                    if split == 'test':
                        keyframes = False
                self.img_sizes = torch.from_numpy(img_sizes)
                poses, imgs, timestamps, self.median_imgs = load_llffvideo_data(
                    videopaths=videopaths, cam_poses=per_cam_poses, img_sizes=self.img_sizes,
                    split=split, keyframes=keyframes, keyframes_take_each=30)
                
                
                self.poses = poses.float()
                self.cam_nums = cam_nums
                self.per_cam_near_fars = torch.tensor(
                        # [[2.0, 6.0]]).repeat(per_cam_poses.shape[0], 1)
                        [[0.1, 6.0]]).repeat(per_cam_poses.shape[0], 1)
            # Normalize timestamps between -1, 1
            self.num_frames = torch.max(timestamps).item() + 1
            print(f"Number of frames: {self.num_frames}")
            timestamps = (timestamps.float() / (self.num_frames-1)) * 2 - 1
        else:
            raise ValueError(datadir)
        intrinsics = torch.from_numpy(intrinsics).float()

        self.timestamps = timestamps * self.normalize_scale
        if split in ['train', 'test_optim']:
            self.timestamps = self.timestamps[:, None, None].repeat(
                1, self.img_sizes[0][1].item(), self.img_sizes[0][0].item()).reshape(-1)  # [n_frames * h * w]

        assert self.timestamps.min() >= -self.normalize_scale and self.timestamps.max() <= self.normalize_scale, "timestamps out of range."
        if imgs is not None and imgs.dtype != torch.uint8:
            imgs = (imgs * 255).to(torch.uint8)
        if self.median_imgs is not None and self.median_imgs.dtype != torch.uint8:
            self.median_imgs = (self.median_imgs * 255).to(torch.uint8)
        if split in ['train', 'test_optim']:
            imgs = imgs.view(-1, imgs.shape[-1])
        elif imgs is not None:
            imgs = imgs.view(-1, self.img_sizes[0][1].item(), self.img_sizes[0][0].item(), imgs.shape[-1])

        # ISG/IST weights are computed on 4x subsampled data.
        weights_subsampled = int(4 / downsample)
        if scene_bbox is not None:
            scene_bbox = torch.tensor(scene_bbox)
        else:
            scene_bbox = get_bbox(datadir, is_contracted=contraction, dset_type=dset_type)
        super().__init__(
            datadir=datadir,
            split=split,
            batch_size=batch_size,
            is_ndc=ndc,
            is_contracted=contraction,
            scene_bbox=scene_bbox,
            rays_o=None,
            rays_d=None,
            intrinsics=intrinsics,
            img_sizes=img_sizes,
            imgs=imgs,
            sampling_weights=None,  # Start without importance sampling, by default
            weights_subsampled=weights_subsampled,
        )

        self.isg_weights = None
        self.ist_weights = None
        if split == "train" and (dset_type == 'llff'):  # Only use importance sampling with DyNeRF videos
            if os.path.exists(os.path.join(datadir, f"isg_weights.pt")):
                self.isg_weights = torch.load(os.path.join(datadir, f"isg_weights.pt"))
                log.info(f"Reloaded {self.isg_weights.shape[0]} ISG weights from file.")
            else:
                # Precompute ISG weights
                t_s = time.time()
                gamma = 1e-3 if self.keyframes else 2e-2
                self.isg_weights = dynerf_isg_weight(
                    imgs.view(-1, intrinsics.height, intrinsics.width, imgs.shape[-1]),
                    median_imgs=self.median_imgs, gamma=gamma)
                # Normalize into a probability distribution, to speed up sampling
                self.isg_weights = (self.isg_weights.reshape(-1) / torch.sum(self.isg_weights))
                torch.save(self.isg_weights, os.path.join(datadir, f"isg_weights.pt"))
                t_e = time.time()
                log.info(f"Computed {self.isg_weights.shape[0]} ISG weights in {t_e - t_s:.2f}s.")

            if os.path.exists(os.path.join(datadir, f"ist_weights.pt")):
                self.ist_weights = torch.load(os.path.join(datadir, f"ist_weights.pt"))
                log.info(f"Reloaded {self.ist_weights.shape[0]} IST weights from file.")
            else:
                # Precompute IST weights
                t_s = time.time()
                self.ist_weights = dynerf_ist_weight(
                    imgs.view(-1, self.img_h, self.img_w, imgs.shape[-1]),
                    num_cameras=self.median_imgs.shape[0])
                # Normalize into a probability distribution, to speed up sampling
                self.ist_weights = (self.ist_weights.reshape(-1) / torch.sum(self.ist_weights))
                torch.save(self.ist_weights, os.path.join(datadir, f"ist_weights.pt"))
                t_e = time.time()
                log.info(f"Computed {self.ist_weights.shape[0]} IST weights in {t_e - t_s:.2f}s.")

        if split == "test_optim" and (dset_type == 'llff'):  # Only use importance sampling with DyNeRF videos
            if os.path.exists(os.path.join(datadir, f"isg_testoptim_weights.pt")):
                self.isg_weights = torch.load(os.path.join(datadir, f"isg_testoptim_weights.pt"))
                log.info(f"Reloaded {self.isg_weights.shape[0]} ISG test optim weights from file.")
            else:
                # Precompute ISG weights
                t_s = time.time()
                gamma = 1e-3 if self.keyframes else 2e-2
                self.isg_weights = dynerf_isg_weight(
                    imgs.view(-1, intrinsics.height, intrinsics.width, imgs.shape[-1]),
                    median_imgs=self.median_imgs, gamma=gamma)
                # Normalize into a probability distribution, to speed up sampling
                self.isg_weights = (self.isg_weights.reshape(-1) / torch.sum(self.isg_weights))
                torch.save(self.isg_weights, os.path.join(datadir, f"isg_testoptim_weights.pt"))
                t_e = time.time()
                log.info(f"Computed {self.isg_weights.shape[0]} ISG test optim weights in {t_e - t_s:.2f}s.")

            if os.path.exists(os.path.join(datadir, f"ist_testoptim_weights.pt")):
                self.ist_weights = torch.load(os.path.join(datadir, f"ist_testoptim_weights.pt"))
                log.info(f"Reloaded {self.ist_weights.shape[0]} IST test optim weights from file.")
            else:
                # Precompute IST weights
                t_s = time.time()
                self.ist_weights = dynerf_ist_weight(
                    imgs.view(-1, self.img_h, self.img_w, imgs.shape[-1]),
                    num_cameras=self.median_imgs.shape[0])
                # Normalize into a probability distribution, to speed up sampling
                self.ist_weights = (self.ist_weights.reshape(-1) / torch.sum(self.ist_weights))
                torch.save(self.ist_weights, os.path.join(datadir, f"ist_testoptim_weights.pt"))
                t_e = time.time()
                log.info(f"Computed {self.ist_weights.shape[0]} IST test optim weights in {t_e - t_s:.2f}s.")

        if self.isg:
            self.enable_isg()

        log.info(f"VideoDataset contracted={self.is_contracted}, ndc={self.is_ndc}. "
                 f"Loaded {self.split} set from {self.datadir}: "
                 f"{len(self.poses)} images of size {self.img_h}x{self.img_w}. "
                 f"Images loaded: {self.imgs is not None}. "
                 f"{len(torch.unique(timestamps))} timestamps. Near-far: {self.per_cam_near_fars}. "
                 f"ISG={self.isg}, IST={self.ist}, weights_subsampled={self.weights_subsampled}. "
                 f"Sampling without replacement={self.use_permutation}. {intrinsics[0]}")

    def enable_isg(self):
        self.isg = True
        self.ist = False
        self.sampling_weights = self.isg_weights
        log.info(f"Enabled ISG weights.")

    def switch_isg2ist(self):
        self.isg = False
        self.ist = True
        self.sampling_weights = self.ist_weights
        log.info(f"Switched from ISG to IST weights.")

    def __getitem__(self, index):
        h = self.img_h
        w = self.img_w
        dev = "cpu"

        if self.split in ['train', 'test_optim']:
            index = self.get_rand_ids(index)  # [batch_size // (weights_subsampled**2)]
            # np.save('get_rand_ids.npy', index)
            if self.weights_subsampled == 1 or self.sampling_weights is None:
                # Nothing special to do, either weights_subsampled = 1, or not using weights.
                image_id = torch.div(index, h * w, rounding_mode='floor')
                y = torch.remainder(index, h * w).div(w, rounding_mode='floor')
                x = torch.remainder(index, h * w).remainder(w)
            else:
                # We must deal with the fact that ISG/IST weights are computed on a dataset with
                # different 'downsampling' factor. E.g. if the weights were computed on 4x
                # downsampled data and the current dataset is 2x downsampled, `weights_subsampled`
                # will be 4 / 2 = 2.
                # Split each subsampled index into its 16 components in 2D.
                hsub, wsub = h // self.weights_subsampled, w // self.weights_subsampled
                image_id = torch.div(index, hsub * wsub, rounding_mode='floor')
                ysub = torch.remainder(index, hsub * wsub).div(wsub, rounding_mode='floor')
                xsub = torch.remainder(index, hsub * wsub).remainder(wsub)
                # xsub, ysub is the first point in the 4x4 square of finely sampled points
                x, y = [], []
                for ah in range(self.weights_subsampled):
                    for aw in range(self.weights_subsampled):
                        x.append(xsub * self.weights_subsampled + aw)
                        y.append(ysub * self.weights_subsampled + ah)
                x = torch.cat(x)
                y = torch.cat(y)
                image_id = image_id.repeat(self.weights_subsampled ** 2)
                # Inverse of the process to get x, y from index. image_id stays the same.
                index = x + y * w + image_id * h * w
            x, y = x + 0.5, y + 0.5

        else:
            image_id = [index]
            x, y = create_meshgrid(height=h, width=w, dev=dev, add_half=True, flat=True)

        out = {
            "timestamps": self.timestamps[index],      # (num_rays or 1, )
            "imgs": None,
        }

        if self.split in ['train', 'test_optim']:
            num_frames_per_camera = len(self.imgs) // (len(self.per_cam_near_fars) * h * w)
            camera_id = torch.div(image_id, num_frames_per_camera, rounding_mode='floor')  # (num_rays)
            out['camids'] = camera_id.long() + 1
            out['near_fars'] = self.per_cam_near_fars[camera_id, :]
        else:
            out['near_fars'] = self.per_cam_near_fars  # Only one test camera
            out['camids'] = (torch.ones([len(image_id)])*self.hold_id).long()

        if self.imgs is not None:
            out['imgs'] = (self.imgs[index] / 255.0).view(-1, self.imgs.shape[-1])

        c2w = self.poses[image_id]                                    # [num_rays or 1, 3, 4]

        all_intrinsics = self.intrinsics[out['camids']]
        camera_dirs = stack_camera_dirs(x, y, all_intrinsics, True)  # [num_rays, 3]
        out['rays_o'], out['rays_d'] = get_rays(
            camera_dirs, c2w, ndc=self.is_ndc, ndc_near=1.0, intrinsics=all_intrinsics,
            normalize_rd=True)                                        # [num_rays, 3]

        imgs = out['imgs']
        # Decide BG color
        bg_color = torch.ones((1, 3), dtype=torch.float32, device=dev)
        if self.split in ['train','test_optim'] and imgs.shape[-1] == 4:
            bg_color = torch.rand((1, 3), dtype=torch.float32, device=dev)
        out['bg_color'] = bg_color
        # Alpha compositing
        if imgs is not None and imgs.shape[-1] == 4:
            imgs = imgs[:, :3] * imgs[:, 3:] + bg_color * (1.0 - imgs[:, 3:])
        out['imgs'] = imgs
        
        return out


def get_bbox(datadir: str, dset_type: str, is_contracted=False) -> torch.Tensor:
    """Returns a default bounding box based on the dataset type, and contraction state.

    Args:
        datadir (str): Directory where data is stored
        dset_type (str): A string defining dataset type (e.g. synthetic, llff)
        is_contracted (bool): Whether the dataset will use contraction

    Returns:
        Tensor: 3x2 bounding box tensor
    """
    if is_contracted:
        radius = 2
    elif dset_type == 'synthetic':
        radius = 1.5
    elif dset_type == 'llff':
        return torch.tensor([[-3.0, -1.67, -1.2], [3.0, 1.67, 1.2]])
    else:
        radius = 1.3
    return torch.tensor([[-radius, -radius, -radius], [radius, radius, radius]])


def fetch_360vid_info(frame: Dict[str, Any]):
    timestamp = None
    fp = frame['file_path']
    if '_r' in fp:
        timestamp = int(fp.split('t')[-1].split('_')[0])
    if 'r_' in fp:
        pose_id = int(fp.split('r_')[-1])
    else:
        pose_id = int(fp.split('r')[-1])
    if timestamp is None:  # will be None for dnerf
        timestamp = frame['time']
    return timestamp, pose_id


def load_360video_frames(datadir, split, max_cameras: int, max_tsteps: Optional[int]) -> Tuple[Any, Any]:
    with open(os.path.join(datadir, f"transforms_{split}.json"), 'r') as fp:
        meta = json.load(fp)
    frames = meta['frames']

    timestamps = set()
    pose_ids = set()
    fpath2poseid = defaultdict(list)
    for frame in frames:
        timestamp, pose_id = fetch_360vid_info(frame)
        timestamps.add(timestamp)
        pose_ids.add(pose_id)
        fpath2poseid[frame['file_path']].append(pose_id)
    timestamps = sorted(timestamps)
    pose_ids = sorted(pose_ids)

    if max_cameras is not None:
        num_poses = min(len(pose_ids), max_cameras or len(pose_ids))
        subsample_poses = int(round(len(pose_ids) / num_poses))
        pose_ids = set(pose_ids[::subsample_poses])
        log.info(f"Selected subset of {len(pose_ids)} camera poses: {pose_ids}.")

    if max_tsteps is not None:
        num_timestamps = min(len(timestamps), max_tsteps or len(timestamps))
        subsample_time = int(math.floor(len(timestamps) / (num_timestamps - 1)))
        timestamps = set(timestamps[::subsample_time])
        log.info(f"Selected subset of timestamps: {sorted(timestamps)} of length {len(timestamps)}")

    sub_frames = []
    for frame in frames:
        timestamp, pose_id = fetch_360vid_info(frame)
        if timestamp in timestamps and pose_id in pose_ids:
            sub_frames.append(frame)
    # We need frames to be sorted by pose_id
    sub_frames = sorted(sub_frames, key=lambda f: fpath2poseid[f['file_path']])
    return sub_frames, meta


def load_llffvideo_poses(datadir: str,
                         downsample: float,
                         split: str,
                         hold_id: 0,
                         near_scaling: float) -> Tuple[
                            torch.Tensor, torch.Tensor, Intrinsics, List[str]]:
    """Load poses and metadata for LLFF video.

    Args:
        datadir (str): Directory containing the videos and pose information
        downsample (float): How much to downsample videos. The default for LLFF videos is 2.0
        split (str): 'train' or 'test'.
        hold_id: Default is 0(test cam), change hold_id to render train view
        near_scaling (float): How much to scale the near bound of poses.

    Returns:
        Tensor: A tensor of size [N, 4, 4] containing c2w poses for each camera.
        Tensor: A tensor of size [N, 2] containing near, far bounds for each camera.
        Intrinsics: The camera intrinsics. These are the same for every camera.
        List[str]: List of length N containing the path to each camera's data.
    """
    poses, near_fars, intrinsics = load_llff_poses_helper(datadir, downsample, near_scaling)

    videopaths = np.array(glob.glob(os.path.join(datadir, '*.mp4')))  # [n_cameras]
    assert poses.shape[0] == len(videopaths), \
        'Mismatch between number of cameras and number of poses!'
    videopaths.sort()
    cam_nums = len(videopaths)

    # The first camera is reserved for testing, following https://github.com/facebookresearch/Neural_3D_Video/releases/tag/v1.0
    if split == 'train':
        split_ids = np.arange(1, poses.shape[0])
    elif split == 'test':
        split_ids = np.array([hold_id])
        print('split_ids:', split_ids)
    else:
        split_ids = np.arange(poses.shape[0])

    poses = torch.from_numpy(poses[split_ids])
    near_fars = torch.from_numpy(near_fars[split_ids])
    videopaths = videopaths[split_ids].tolist()
    
    return poses, near_fars, intrinsics, videopaths, cam_nums


def load_llffvideo_poses_testoptim(datadir: str,
                         downsample: float,
                         split: str,
                         hold_id: 0,
                         near_scaling: float) -> Tuple[
                            torch.Tensor, torch.Tensor, Intrinsics, List[str]]:
    """Load poses and metadata for LLFF video.

    Args:
        datadir (str): Directory containing the videos and pose information
        downsample (float): How much to downsample videos. The default for LLFF videos is 2.0
        split (str): 'train' or 'test'.
        hold_id: Default is 0(test cam), change hold_id to render train view
        near_scaling (float): How much to scale the near bound of poses.

    Returns:
        Tensor: A tensor of size [N, 4, 4] containing c2w poses for each camera.
        Tensor: A tensor of size [N, 2] containing near, far bounds for each camera.
        Intrinsics: The camera intrinsics. These are the same for every camera.
        List[str]: List of length N containing the path to each camera's data.
    """
    poses, near_fars, intrinsics = load_llff_poses_helper(datadir, downsample, near_scaling)

    videopaths = np.array(glob.glob(os.path.join(datadir, '*.mp4')))  # [n_cameras]
    assert poses.shape[0] == len(videopaths), \
        'Mismatch between number of cameras and number of poses!'
    videopaths.sort()
    
    # The first camera is reserved for testing, following https://github.com/facebookresearch/Neural_3D_Video/releases/tag/v1.0
    if split == 'train':
        split_ids = np.array([hold_id])
    elif split == 'test':
        split_ids = np.array([hold_id])
        print('split_ids:', split_ids)
    else: # 'test_optim'
        split_ids = np.array([hold_id])

    poses = torch.from_numpy(poses[split_ids])
    near_fars = torch.from_numpy(near_fars[split_ids])
    videopaths = videopaths[split_ids].tolist()
    cam_nums = len(videopaths)
    
    return poses, near_fars, intrinsics, videopaths, cam_nums


def load_llffvideo_data(videopaths: List[str],
                        cam_poses: torch.Tensor,
                        img_sizes,
                        split: str,
                        keyframes: bool,
                        keyframes_take_each: Optional[int] = None,
                        ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    if keyframes and (keyframes_take_each is None or keyframes_take_each < 1):
        raise ValueError(f"'keyframes_take_each' must be a positive number, "
                         f"but is {keyframes_take_each}.")

    loaded = parallel_load_images(
        dset_type="video",
        tqdm_title=f"Loading {split} data",
        num_images=len(videopaths),
        paths=videopaths,
        poses=cam_poses,
        img_sizes=img_sizes,
        load_every=keyframes_take_each if keyframes else 1,
    )
    imgs, poses, median_imgs, timestamps = zip(*loaded)
    # Stack everything together
    timestamps = torch.cat(timestamps, 0)  # [N]
    poses = torch.cat(poses, 0)            # [N, 3, 4]
    imgs = torch.cat(imgs, 0)              # [N, h, w, 3]
    median_imgs = torch.stack(median_imgs, 0)  # [num_cameras, h, w, 3]

    return poses, imgs, timestamps, median_imgs

def load_blendervideo_poses_custom(datadir: str,
                         downsample: float,
                         split: str,
                         hold_id:0,
                         video: str,
                         near_scaling: float) -> Tuple[
                            torch.Tensor, torch.Tensor, Intrinsics, List[str]]:
    """Load poses and metadata for LLFF video.

    Args:
        datadir (str): Directory containing the videos and pose information
        downsample (float): How much to downsample videos. The default for LLFF videos is 2.0
        split (str): 'train' or 'test'.
        hold_id: Default is 0(test cam), change hold_id to render train view
        near_scaling (float): How much to scale the near bound of poses.

    Returns:
        Tensor: A tensor of size [N, 4, 4] containing c2w poses for each camera.
        Intrinsics: The camera intrinsics. These are the same for every camera.
        List[str]: List of length N containing the path to each camera's data.
    """
    #poses, intrinsics = load_llff_poses_helper(datadir, downsample, near_scaling)

    dir_paths = sorted([x for x in os.listdir(datadir) if video in x])
    dir_paths.insert(0, dir_paths[0])

    # intrinsics = []
    # videopaths = []
    # poses = []

    # for dir_path in dir_paths:

    #     c2w= os.path.join(datadir, dir_path, "c2w_centered.npy")
    #     intrinsic = os.path.join(datadir, dir_path, "K_syncnerf.npy")
    #     video_path = os.path.join(datadir, dir_path, "video_frames.mp4")
    #     img_dim_path = os.path.join(datadir, dir_path, "image_dims.npy")

    #     videopaths.append(video_path)
    #     intrinsics.append(np.load(intrinsic))
    #     poses.append(np.load(c2w))

    # intrinsics = np.stack(intrinsics)
    # poses = np.stack(poses)

    # poses = poses[:,0]
    # intrinsics = intrinsics[0]

    # fx = intrinsics[0, 0]
    # fy = intrinsics[1, 1]
    # cx = intrinsics[0, 2]
    # cy = intrinsics[1, 2]

    # dim = np.load(img_dim_path)

    # intrinsics = Intrinsics(height=dim[1], width=dim[0], focal_x=fx, focal_y=fy, center_x=cx, center_y=cy)

    intrinsics = []
    videopaths = []
    poses = []
    img_sizes = []

    for dir_path in dir_paths:

        c2w= os.path.join(datadir, dir_path, "c2w_centered.npy")
        intrinsic = os.path.join(datadir, dir_path, "K_syncnerf.npy")
        video_path = os.path.join(datadir, dir_path, "video_frames.mp4")
        img_dim_path = os.path.join(datadir, dir_path, "image_dims.npy")

        videopaths.append(video_path)
        K = np.load(intrinsic)

        dim = np.load(img_dim_path)
        img_sizes.append(dim)

        intrinsics.append(K)
        poses.append(np.load(c2w))

    poses = np.stack(poses)
    img_sizes = np.stack(img_sizes)
    intrinsics = np.stack(intrinsics)

    poses = poses[:,0]

    videopaths = np.array(videopaths)  # [n_cameras]
    assert poses.shape[0] == len(videopaths), \
        'Mismatch between number of cameras and number of poses!'
    videopaths.sort()
    cam_nums = len(videopaths)
    if split == 'train':
        # split_ids = np.arange(poses.shape[0])
        split_ids = np.arange(1, poses.shape[0])
    elif split == 'test':
        split_ids = np.array([hold_id])
    else:
        split_ids = np.arange(poses.shape[0])

    poses = torch.from_numpy(poses[split_ids])
    videopaths = videopaths[split_ids].tolist()

    return poses, intrinsics, videopaths, cam_nums, img_sizes

def load_blendervideo_poses(datadir: str,
                         downsample: float,
                         split: str,
                         hold_id:0,
                         near_scaling: float) -> Tuple[
                            torch.Tensor, torch.Tensor, Intrinsics, List[str]]:
    """Load poses and metadata for LLFF video.

    Args:
        datadir (str): Directory containing the videos and pose information
        downsample (float): How much to downsample videos. The default for LLFF videos is 2.0
        split (str): 'train' or 'test'.
        hold_id: Default is 0(test cam), change hold_id to render train view
        near_scaling (float): How much to scale the near bound of poses.

    Returns:
        Tensor: A tensor of size [N, 4, 4] containing c2w poses for each camera.
        Intrinsics: The camera intrinsics. These are the same for every camera.
        List[str]: List of length N containing the path to each camera's data.
    """
    #poses, intrinsics = load_llff_poses_helper(datadir, downsample, near_scaling)
    with open(os.path.join(datadir, f"transforms.json"), 'r') as f:
        transform = json.load(f)
    poses = []
    for i in range(len(transform['frames'])):
        poses.append(transform['frames'][i]['transform_matrix'])
    poses = np.stack(poses)
    width = transform['width']
    height = transform['height']
    fl_x = width / (2 * np.tan(transform['camera_angle_x'] / 2)) 
    fl_y = fl_x
    cx = (transform['cx'] / downsample) if 'cx' in transform else (width / 2)
    cy = (transform['cy'] / downsample) if 'cy' in transform else (height / 2)
    intrinsics = Intrinsics(height=height, width=width, focal_x=fl_x, focal_y=fl_y, center_x=cx, center_y=cy)
    videopaths = np.array(glob.glob(os.path.join(datadir, '*.mp4')))  # [n_cameras]

    assert poses.shape[0] == len(videopaths), \
        'Mismatch between number of cameras and number of poses!'
    videopaths.sort()
    cam_nums = len(videopaths)

    # The first camera is reserved for testing, following https://github.com/facebookresearch/Neural_3D_Video/releases/tag/v1.0
    if split == 'train':
        split_ids = np.arange(1, poses.shape[0])
    elif split == 'test':
        split_ids = np.array([hold_id])
    else:
        split_ids = np.arange(poses.shape[0])

    poses = torch.from_numpy(poses[split_ids])
    videopaths = videopaths[split_ids].tolist()

    return poses, intrinsics, videopaths, cam_nums

def load_blendervideo_poses_testoptim(datadir: str,
                         downsample: float,
                         split: str,
                         hold_id:0,
                         near_scaling: float) -> Tuple[
                            torch.Tensor, torch.Tensor, Intrinsics, List[str]]:
    """Load poses and metadata for LLFF video.

    Args:
        datadir (str): Directory containing the videos and pose information
        downsample (float): How much to downsample videos. The default for LLFF videos is 2.0
        split (str): 'train' or 'test'.
        hold_id: Default is 0(test cam), change hold_id to render train view
        near_scaling (float): How much to scale the near bound of poses.

    Returns:
        Tensor: A tensor of size [N, 4, 4] containing c2w poses for each camera.
        Intrinsics: The camera intrinsics. These are the same for every camera.
        List[str]: List of length N containing the path to each camera's data.
    """
    #poses, intrinsics = load_llff_poses_helper(datadir, downsample, near_scaling)
    with open(os.path.join(datadir, f"transforms.json"), 'r') as f:
        transform = json.load(f)
    poses = []
    for i in range(len(transform['frames'])):
        poses.append(transform['frames'][i]['transform_matrix'])
    poses = np.stack(poses)
    width = transform['width']
    height = transform['height']
    fl_x = width / (2 * np.tan(transform['camera_angle_x'] / 2)) 
    fl_y = fl_x
    cx = (transform['cx'] / downsample) if 'cx' in transform else (width / 2)
    cy = (transform['cy'] / downsample) if 'cy' in transform else (height / 2)
    intrinsics = Intrinsics(height=height, width=width, focal_x=fl_x, focal_y=fl_y, center_x=cx, center_y=cy)
    videopaths = np.array(glob.glob(os.path.join(datadir, '*.mp4')))  # [n_cameras]

    assert poses.shape[0] == len(videopaths), \
        'Mismatch between number of cameras and number of poses!'
    videopaths.sort()

    # The first camera is reserved for testing, following https://github.com/facebookresearch/Neural_3D_Video/releases/tag/v1.0
    split_ids = np.array([hold_id])

    poses = torch.from_numpy(poses[split_ids])
    videopaths = videopaths[split_ids].tolist()
    cam_nums = len(videopaths)
    return poses, intrinsics, videopaths, cam_nums

@torch.no_grad()
def dynerf_isg_weight(imgs, median_imgs, gamma):
    # imgs is [num_cameras * num_frames, h, w, 3]
    # median_imgs is [num_cameras, h, w, 3]
    assert imgs.dtype == torch.uint8
    assert median_imgs.dtype == torch.uint8
    num_cameras, h, w, c = median_imgs.shape
    squarediff = (
        imgs.view(num_cameras, -1, h, w, c)
            .float()  # creates new tensor, so later operations can be in-place
            .div_(255.0)
            .sub_(
                median_imgs[:, None, ...].float().div_(255.0)
            )
            .square_()  # noqa
    )  # [num_cameras, num_frames, h, w, 3]
    # differences = median_imgs[:, None, ...] - imgs.view(num_cameras, -1, h, w, c)  # [num_cameras, num_frames, h, w, 3]
    # squarediff = torch.square_(differences)
    psidiff = squarediff.div_(squarediff + gamma**2)
    psidiff = (1./3) * torch.sum(psidiff, dim=-1)  # [num_cameras, num_frames, h, w]
    return psidiff  # valid probabilities, each in [0, 1]


@torch.no_grad()
def dynerf_ist_weight(imgs, num_cameras, alpha=0.1, frame_shift=25):  # DyNerf uses alpha=0.1
    assert imgs.dtype == torch.uint8
    N, h, w, c = imgs.shape
    frames = imgs.view(num_cameras, -1, h, w, c).float()  # [num_cameras, num_timesteps, h, w, 3]
    max_diff = None
    shifts = list(range(frame_shift + 1))[1:]
    for shift in shifts:
        shift_left = torch.cat([frames[:, shift:, ...], torch.zeros(num_cameras, shift, h, w, c)], dim=1)
        shift_right = torch.cat([torch.zeros(num_cameras, shift, h, w, c), frames[:, :-shift, ...]], dim=1)
        mymax = torch.maximum(torch.abs_(shift_left - frames), torch.abs_(shift_right - frames))
        if max_diff is None:
            max_diff = mymax
        else:
            max_diff = torch.maximum(max_diff, mymax)  # [num_timesteps, h, w, 3]
    max_diff = torch.mean(max_diff, dim=-1)  # [num_timesteps, h, w]
    max_diff = max_diff.clamp_(min=alpha)
    return max_diff
