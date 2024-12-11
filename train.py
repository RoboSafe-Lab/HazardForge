import os
import argparse
import  sys
import pytorch_lightning as pl

from tbsim.utils.log_utils import PrintLogger
from tbsim.utils.batch_utils import set_global_batch_type
from tbsim.utils.trajdata_utils import set_global_trajdata_batch_env, set_global_trajdata_batch_raster_cfg
import tbsim.utils.train_utils as TrainUtils
from tbsim.datasets.factory import datamodule_factory
from tbsim.utils.env_utils import RolloutCallback

import wandb,json
from pytorch_lightning.loggers import  WandbLogger
from  models.algos import  UnifiedTrainer
from datetime import  datetime
from configs.custom_config import dict_to_config,ConfigBase,serialize_object
from src.tbsim.configs.base import ExperimentConfig
import yaml

def main(cfg, auto_remove_exp_dir, debug=False):
    pl.seed_everything(cfg.seed)
    set_global_batch_type("trajdata")
    set_global_trajdata_batch_env(cfg.train.trajdata_source_train[0])
    set_global_trajdata_batch_raster_cfg(cfg.env.rasterizer)
    print("\n============= New Training Run with Config =============")

    root_dir, log_dir, ckpt_dir, video_dir, version_key = TrainUtils.get_exp_dir(
        exp_name=cfg.name,
        output_dir=cfg.root_dir,
        save_checkpoints=cfg.train.save.enabled,
        auto_remove_exp_dir=auto_remove_exp_dir
    )
    with open(os.path.join(root_dir, version_key, "config.json"), "w") as f:
        json.dump(serialize_object(default_config), f, indent=4)

    if cfg.train.logging.terminal_output_to_txt and not debug:

        logger = PrintLogger(os.path.join(log_dir, "log.txt"))
        sys.stdout = logger
        sys.stderr = logger
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    train_callbacks = []
    # Training Parallelism
    #     with cfg.train.validation.unlocked():
    #         cfg.train.validation
    # assert cfg.train.parallel_strategy in [
    #     "dp",
    #     "ddp_spawn",
    #     None,
    # ]
    # if not cfg.devices.num_gpus > 1:
    #
    #     with cfg.train.unlocked():
    #         cfg.train.parallel_strategy = None
    # if cfg.train.parallel_strategy in ["ddp_spawn"]:
    #     with cfg.train.training.unlocked():
    #         cfg.train.training.batch_size = int(
    #             cfg.train.training.batch_size / cfg.devices.num_gpus
    #         ).batch_size = int(
    #             cfg.train.validation.batch_size / cfg.devices.num_gpus
    #         )

    datamodule = datamodule_factory(
        cls_name=cfg.train.datamodule_class, config=cfg
    )
    datamodule.setup()

    # Environment for close-loop evaluation
    if cfg.train.rollout.enabled:
        # Run rollout at regular intervals
        rollout_callback = RolloutCallback(
            exp_config=cfg,
            every_n_steps=cfg.train.rollout.every_n_steps,
            warm_start_n_steps=cfg.train.rollout.warm_start_n_steps,
            verbose=True,
            save_video=cfg.train.rollout.save_video,
            video_dir=video_dir
        )
        train_callbacks.append(rollout_callback)
    model = UnifiedTrainer(algo_config=cfg.algo,train_config=cfg.train,
                           modality_shapes=datamodule.modality_shapes,
                           registered_name=cfg.registered_name,
                           train_mode=cfg.train.mode)
    # Checkpointing
    if cfg.train.validation.enabled and cfg.train.save.save_best_validation:#NOTE:  first validation then save
        assert (cfg.train.save.every_n_steps > cfg.train.validation.every_n_steps),"checkpointing frequency (" + str(
            cfg.train.save.every_n_steps) + ") needs to be greater than validation frequency (" + str(cfg.train.validation.every_n_steps) + ")"
        
        
        for metric_name, metric_key in model.checkpoint_monitor_keys.items():
            print(
                "Monitoring metrics {} under alias {}".format(metric_key, metric_name)
            )
            ckpt_valid_callback = pl.callbacks.ModelCheckpoint(
                dirpath=f"{ckpt_dir}/{metric_name}",
                filename=f"iter{{step}}_ep{{epoch}}_{metric_name}_{metric_key}",
                auto_insert_metric_name=False,
                save_top_k=cfg.train.save.best_k,
                monitor=metric_key,
                mode="min",
                every_n_train_steps=cfg.train.save.every_n_steps,
                verbose=True,
            )
            train_callbacks.append(ckpt_valid_callback)
    if cfg.train.rollout.enabled and cfg.train.save.save_best_rollout:
        assert (
            cfg.train.save.every_n_steps > cfg.train.rollout.every_n_steps
        ), "checkpointing frequency needs to be greater than rollout frequency"
        ckpt_rollout_callback = pl.callbacks.ModelCheckpoint(
            dirpath=ckpt_dir,
            filename="iter{step}_ep{epoch}_simADE{rollout/metrics_ego_ADE:.2f}",
            auto_insert_metric_name=False,
            save_top_k=cfg.train.save.best_k,  # save the best k models
            monitor="rollout/metrics_ego_ADE",
            mode="min",
            every_n_train_steps=cfg.train.save.every_n_steps,
            state_key='rollout_checkpoint',
            verbose=True,
        )
        train_callbacks.append(ckpt_rollout_callback)


    logger = None
    if debug:
        print("Debugging mode, suppress logging.")
    elif cfg.train.logging.log_wandb:
        wandb.login()
        logger = WandbLogger(name=f"{cfg.name}_{current_time}",project=cfg.train.logging.wandb_project_name)
        logger.experiment.config.update(cfg.to_dict())
        logger.watch(model=model)


    trainer = pl.Trainer(
        default_root_dir=root_dir,
        # checkpointing
        enable_checkpointing=cfg.train.save.enabled,
        # logging
        logger=logger,
        # flush_logs_every_n_steps=cfg.train.logging.flush_every_n_steps,
        log_every_n_steps=cfg.train.logging.log_every_n_steps,
        # training
        max_steps=cfg.train.training.num_steps,
        # validation
        val_check_interval=cfg.train.validation.every_n_steps,
        limit_val_batches=cfg.train.validation.num_steps_per_epoch,
        # all callbacks
        callbacks=train_callbacks,
        num_sanity_val_steps=0,
    )
    # checkpoint_point = "/home/visier/hazardforge/visier_logs/test/run0/checkpoints/val_loss/iter4000_ep0_val_loss_val_dm_loss.ckpt"
    trainer.fit(model=model, datamodule=datamodule)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Training Script")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="./HazardForge/config.yaml", help="Path to YAML config")
    args = parser.parse_args()
    with open(args.config, "r") as f:
        config_dict = yaml.safe_load(f)
    train_config = dict_to_config(ConfigBase, config_dict.get("train", {}))
    env_config = dict_to_config(ConfigBase, config_dict.get("env", {}))
    algo_config = dict_to_config(ConfigBase, config_dict.get("algo", {}))
    default_config = ExperimentConfig(
        train_config=train_config,
        env_config=env_config,
        algo_config=algo_config,
        registered_name=config_dict.get("registered_name", "default_experiment"),
    )
  

    if default_config.train.rollout.get("enabled", False):
        default_config.env["eval"] = {"env": default_config.env["name"]}
        assert default_config.algo["eval_class"], f"Please set an eval_class for {default_config.algo['name']}"
        default_config.env["eval"]["eval_class"] = default_config.algo["eval_class"]
        default_config.env["eval"]["dataset_path"] = default_config.train["trajdata_data_dirs"]["nusc_trainval"]
        env_specific_config = default_config.env.get(default_config.env["eval"]["env"], {})
        for key, value in env_specific_config.items():
            default_config.env["eval"][key] = value

    default_config.lock()  # Make config read-only
  
    main(default_config, auto_remove_exp_dir=default_config.train.remove_exp_dir, debug=default_config.train.debug)