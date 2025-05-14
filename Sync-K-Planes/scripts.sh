#!/bin/bash

PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/panoptic.py --video pan171026_pose3
PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/panoptic.py --video pan160906_pizza1
PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/panoptic.py --video pan161029_sports1
PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/panoptic.py --video pan171204_pose3

PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/3dpop.py --video sequence60_n01
PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/3dpop.py --video sequence61_n02
PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/3dpop.py --video sequence64_n05
PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/3dpop.py --video sequence68_n02
PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/3dpop.py --video sequence69_n05
PYTHONPATH='.' python plenoxels/main.py --config-path ./plenoxels/configs/blender/hybrid/3dpop.py --video sequence70_n11