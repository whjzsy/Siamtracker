python -m torch.distributed.launch    --nproc_per_node=2  --master_port=2333   train.py --cfg configs/mobilenetv2_config.yaml
