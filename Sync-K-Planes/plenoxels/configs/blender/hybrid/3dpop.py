config = {
#  'expname': 'panoptic',
 'expname': '3dpop',
 'logdir': '/home/dyyao2/Documents/campuscluster/datasets/egohumans/syncnerf_results/',
 'device': 'cuda:0',

 # Run first for 1 step with data_downsample=4 to generate weights for ray importance sampling
 'data_downsample': 1.0,
 'data_dirs': ['/home/dyyao2/Documents/campuscluster/datasets/egohumans/3dpop/data_preprocessed'],
#  'data_dirs': ['/home/dyyao2/Documents/campuscluster/datasets/panoptic-toolbox/data_preprocessed'],
#  'data_dirs': ['/home/dyyao2/Documents/campuscluster/personals/david/Sync-NeRF/data_preprocessed/'],
 'contract': False,
 'ndc': False,
 'isg': False,
 'isg_step': -1,
 'ist_step': -1,
 'keyframes': False,
 'scene_bbox': [[-1.3, -1.3, -1.3], [1.3, 1.3, 1.3]],

 # Optimization settings
 'num_steps': 50001,
 'batch_size': 4096,
 'scheduler_type': 'warmup_cosine',
 'optim_type': 'adam',
 'lr': 0.01,

  #  Time offset settings
 'offset_lambda': 0.1,
 'offset_freeze_iter': 50000,
 'normalize_scale': 0.8,
 'num_frames': 270,
 'l1_offset': True,
 'l1_offset_gamma': 0.0001,


 # Regularization
 'distortion_loss_weight': 0.00,
 'histogram_loss_weight': 1.0,
 'l1_time_planes': 0.0001,
 'l1_time_planes_proposal_net': 0.0001,
 'plane_tv_weight': 0.0001,
 'plane_tv_weight_proposal_net': 0.0001,
 'time_smoothness_weight': 0.01,
 'time_smoothness_weight_proposal_net': 0.001,
 'density_l1': 0.0001,

 # Training settings
 'save_every': 50000,
 'valid_every': 50000,
 'save_outputs': True,
 'train_fp16': True,

 # Raymarching settings
 'single_jitter': False,
 'num_samples': 48,
 'num_proposal_iterations': 2,
 'num_proposal_samples': [256, 128],
 'use_same_proposal_network': False,
 'use_proposal_weight_anneal': True,
'proposal_net_args_list': [
  {'num_input_coords': 4, 'num_output_coords': 8, 'resolution': [64, 64, 64, 300]},
  {'num_input_coords': 4, 'num_output_coords': 8, 'resolution': [128, 128, 128, 300]}
 ],
#  'proposal_net_args_list': [
#   {'num_input_coords': 4, 'num_output_coords': 8, 'resolution': [128, 128, 128, 150]},
#   {'num_input_coords': 4, 'num_output_coords': 8, 'resolution': [256, 256, 256, 150]}
#  ],

 # Model settings
 'concat_features_across_scales': True,
 'density_activation': 'trunc_exp',
 'linear_decoder': False,
 'multiscale_res': [1, 2, 4, 8],
 # Use time reso = half the number of frames
 # Lego: 25 (50 frames)
 # Hell Warrior and Hook: 50 (100 frames)
 # Mutant, Bouncing Balls, and Stand Up: 75 (150 frames)
 # T-Rex and Jumping Jacks: 100 (200 frames)
 'grid_config': [{
  'grid_dimensions': 2,
  'input_coordinate_dim': 4,
  'output_coordinate_dim': 32,
  'resolution': [64, 64, 64, 150]
 }],
}
