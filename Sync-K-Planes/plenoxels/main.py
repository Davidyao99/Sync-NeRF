import argparse
import importlib.util
import logging
import os
import pprint
import sys
from typing import List, Dict, Any
import tempfile
import numpy as np
import torch
import torch.utils.data
from plenoxels.runners import video_trainer
from plenoxels.runners import phototourism_trainer
from plenoxels.runners import static_trainer
from plenoxels.utils.create_rendering import render_to_path, decompose_space_time
from plenoxels.utils.parse_args import parse_optfloat


def setup_logging(log_level=logging.INFO):
    handlers = [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=log_level,
                        format='%(asctime)s|%(levelname)8s| %(message)s',
                        handlers=handlers,
                        force=True)


def load_data(model_type: str, data_downsample, data_dirs, validate_only: bool, render_only: bool, **kwargs):
    data_downsample = parse_optfloat(data_downsample, default_val=1.0)
    return video_trainer.load_data(
        data_downsample, data_dirs, validate_only=validate_only,
        render_only=render_only, **kwargs)


def init_trainer(model_type: str, **kwargs):
    from plenoxels.runners import video_trainer
    return video_trainer.VideoTrainer(**kwargs)


def save_config(config):
    log_dir = os.path.join(config['logdir'], config['expname'])
    os.makedirs(log_dir, exist_ok=True)

    with open(os.path.join(log_dir, 'config.py'), 'wt') as out:
        out.write('config = ' + pprint.pformat(config))

    with open(os.path.join(log_dir, 'config.csv'), 'w') as f:
        for key in config.keys():
            f.write("%s\t%s\n" % (key, config[key]))


def main():
    setup_logging()

    p = argparse.ArgumentParser(description="")

    p.add_argument('--render-only', action='store_true')
    p.add_argument('--validate-only', action='store_true')
    p.add_argument('--spacetime-only', action='store_true')
    p.add_argument('--test-optim', action='store_true')
    p.add_argument('--config-path', type=str, required=True)
    p.add_argument('--log-dir', type=str, default=None)
    p.add_argument('--seed', type=int, default=0)
    p.add_argument('--video', type=str, default=None)
    p.add_argument('override', nargs=argparse.REMAINDER)

    args = p.parse_args()

    # Set random seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    # Import config
    spec = importlib.util.spec_from_file_location(os.path.basename(args.config_path), args.config_path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    config: Dict[str, Any] = cfg.config
    config["video"] = args.video

    if args.video is not None:
        config["expname"] = config["expname"] + "_" + args.video
    # Process overrides from argparse into config
    # overrides can be passed from the command line as key=value pairs. E.g.
    # python plenoxels/main.py --config-path plenoxels/config/cfg.py max_ts_frames=200
    # note that all values are strings, so code should assume incorrect data-types for anything
    # that's derived from config - and should not a string.
    overrides: List[str] = args.override
    overrides_dict = {ovr.split("=")[0]: ovr.split("=")[1] for ovr in overrides}
    # config.update(overrides_dict)
    config.update({'test_optim':args.test_optim})

    model_type = "video"
    validate_only = args.validate_only
    test_optim = args.test_optim
    render_only = args.render_only
    spacetime_only = args.spacetime_only
    
    if validate_only and render_only:
        raise ValueError("render_only and validate_only are mutually exclusive.")
    if render_only and spacetime_only:
        raise ValueError("render_only and spacetime_only are mutually exclusive.")
    if validate_only and spacetime_only:
        raise ValueError("validate_only and spacetime_only are mutually exclusive.")

    pprint.pprint(config)
    if validate_only or render_only:
        args.log_dir = os.path.join(config['logdir'], config['expname'])
        assert args.log_dir is not None and os.path.isdir(args.log_dir)
    else:
        save_config(config)

    data = load_data(model_type, validate_only=validate_only, render_only=render_only or spacetime_only, **config)
    config.update(data)
    trainer = init_trainer(model_type, **config)
    # if render_only is not None:
    #     checkpoint_path = os.path.join(config["logdir"], config["expname"], "model.pth")
    #     training_needed = not (validate_only or render_only or spacetime_only)
    #     trainer.load_model(torch.load(checkpoint_path), training_needed=training_needed)

    if validate_only:
        trainer.validate()
    elif test_optim:
        trainer.train()
    elif render_only:
        render_to_path(trainer, extra_name="")
    elif spacetime_only:
        decompose_space_time(trainer, extra_name="")
    else:
        trainer.train()


if __name__ == "__main__":
    main()