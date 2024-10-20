from functools import partial
from pathlib import Path
import shutil
import time
from typing import List, Optional, Tuple

from hydra.utils import instantiate
import numpy as np
from omegaconf import DictConfig, OmegaConf
import torch
import torch.distributed as dist
from torch.utils.data import DataLoader
from tqdm import tqdm, trange
import wandb
import logging

logger = logging.getLogger(__name__)
import os

from agent import Agent
from coroutines.collector import make_collector, NumToCollect
from data import BatchSampler, collate_segments_to_batch, Dataset, DatasetTraverser
from envs import make_atari_env, WorldModelEnv
from utils import (
    broadcast_if_needed,
    build_ddp_wrapper,
    CommonTools,
    configure_opt,
    count_parameters,
    get_lr_sched,
    keep_agent_copies_every,
    Logs,
    process_confusion_matrices_if_any_and_compute_classification_metrics,
    save_info_for_import_script,
    save_with_backup,
    set_seed,
    StateDictMixin,
    try_until_no_except,
    wandb_log,
)


# Check if WANDB_API_KEY is set
if 'WANDB_API_KEY' not in os.environ:
    logger.warning("WANDB_API_KEY is not set in the environment variables. Logs may not be sent to wandb.")
else:
    logger.info("WANDB_API_KEY is set. Logs will be sent to wandb.")


class Trainer(StateDictMixin):
    def __init__(self, cfg: DictConfig, root_dir: Path) -> None:
        torch.backends.cuda.matmul.allow_tf32 = True
        OmegaConf.resolve(cfg)
        self._cfg = cfg
        self._rank = dist.get_rank() if dist.is_initialized() else 0
        self._world_size = dist.get_world_size() if dist.is_initialized() else 1

        # Pick a random seed
        set_seed(torch.seed() % 10 ** 9)

        # Device
        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu", self._rank)
        print(f"Starting on {self._device}")
        self._use_cuda = self._device.type == "cuda"
        if self._use_cuda:
            torch.cuda.set_device(self._rank)  # fix compilation error on multi-gpu nodes

        # Init wandb
        if self._rank == 0:
            logger.info(f"Wandb config from cfg.wandb: {cfg.wandb}")
            
            if cfg.wandb.get('mode') == 'disabled':
                logger.info("Wandb is disabled in the configuration. No logs will be sent to wandb.")
                wandb.init(mode="disabled")
            else:
                try:
                    # Ensure WANDB_API_KEY is set in the environment
                    if 'WANDB_API_KEY' not in os.environ:
                        logger.warning("WANDB_API_KEY is not set in the environment variables. Logs may not be sent to wandb.")
                    else:
                        logger.info(f"WANDB_API_KEY is set. Value: {os.environ['WANDB_API_KEY'][:5]}...")

                    logger.info("Initializing wandb...")
                    wandb_config = OmegaConf.to_container(cfg, resolve=True)
                    
                    wandb.init(
                        config=wandb_config,
                        reinit=True,
                        resume=True, 
                        **cfg.wandb
                    )
                    
                    if wandb.run is not None:
                        logger.info(f"Wandb run initialized. Run ID: {wandb.run.id}")
                        logger.info(f"Wandb run name: {wandb.run.name}")
                        logger.info(f"Wandb project: {wandb.run.project}")
                        logger.info(f"Wandb entity: {wandb.run.entity}")
                        if wandb.run.url:
                            logger.info(f"View logs at: {wandb.run.url}")
                            print(f"\nWandb run initialized. View logs at: {wandb.run.url}\n")
                        else:
                            logger.warning("Wandb run URL is not available.")
                    else:
                        logger.warning("Wandb run is None after initialization.")
                except Exception as e:
                    logger.error(f"Failed to initialize wandb: {str(e)}", exc_info=True)
                    print("Wandb initialization failed. Logs will not be available.")
                    wandb.init(mode="disabled")
                    raise e

        # Flags
        self._is_static_dataset = cfg.static_dataset.path is not None
        self._is_model_free = cfg.training.model_free

        # Checkpointing
        self._path_ckpt_dir = Path("checkpoints")
        self._path_state_ckpt = self._path_ckpt_dir / "state.pt"
        self._keep_agent_copies = partial(
            keep_agent_copies_every,
            every=cfg.checkpointing.save_agent_every,
            path_ckpt_dir=self._path_ckpt_dir,
            num_to_keep=cfg.checkpointing.num_to_keep,
        )
        self._save_info_for_import_script = partial(
            save_info_for_import_script, run_name=cfg.wandb.name, path_ckpt_dir=self._path_ckpt_dir
        )

        # First time, init files hierarchy
        if not cfg.common.resume and self._rank == 0:
            self._path_ckpt_dir.mkdir(exist_ok=False, parents=False)
            path_config = Path("config") / "trainer.yaml"
            path_config.parent.mkdir(exist_ok=False, parents=False)
            shutil.move(".hydra/config.yaml", path_config)
             # Move this line after wandb.init()
            if wandb.run is not None:
                wandb.save(str(path_config))
            else:
                logger.warning("wandb.run is None. Config file not saved to wandb.")
            shutil.copytree(src=root_dir / "src", dst="./src")
            shutil.copytree(src=root_dir / "scripts", dst="./scripts")

        # Datasets
        num_workers = cfg.training.num_workers_data_loaders
        use_manager = cfg.training.cache_in_ram and (num_workers > 0)
        p = Path(cfg.static_dataset.path) if self._is_static_dataset else Path("dataset")
        self.train_dataset = Dataset(p / "train", "train_dataset", cfg.training.cache_in_ram, use_manager)
        self.test_dataset = Dataset(p / "test", "test_dataset", cache_in_ram=True)
        self.train_dataset.load_from_default_path()
        self.test_dataset.load_from_default_path()

        logger.info(f"Train dataset path: {self.train_dataset._directory}")
        logger.info(f"Test dataset path: {self.test_dataset._directory}")

        # Envs
        if self._rank == 0:
            train_env = make_atari_env(num_envs=cfg.collection.train.num_envs, device=self._device, **cfg.env.train)
            test_env = make_atari_env(num_envs=cfg.collection.test.num_envs, device=self._device, **cfg.env.test)
            num_actions = int(test_env.num_actions)
        else:
            num_actions = None
        num_actions, = broadcast_if_needed(num_actions)

        # Create models
        self.agent = Agent(instantiate(cfg.agent, num_actions=num_actions)).to(self._device)
        self._agent = build_ddp_wrapper(**self.agent._modules) if dist.is_initialized() else self.agent

        if cfg.initialization.path_to_ckpt is not None:
            self.agent.load(**cfg.initialization)

        # Collectors
        if not self._is_static_dataset and self._rank == 0:
            self._train_collector = make_collector(
                train_env, self.agent.actor_critic, self.train_dataset, cfg.collection.train.epsilon
            )
            self._test_collector = make_collector(
                test_env, self.agent.actor_critic, self.test_dataset, cfg.collection.test.epsilon, reset_every_collect=True
            )

        ######################################################

        # Optimizers and LR schedulers

        def build_opt(name: str) -> torch.optim.AdamW:
            return configure_opt(getattr(self.agent, name), **getattr(cfg, name).optimizer)

        def build_lr_sched(name: str) -> torch.optim.lr_scheduler.LambdaLR:
            return get_lr_sched(self.opt.get(name), getattr(cfg, name).training.lr_warmup_steps)

        self._model_names = ["denoiser", "rew_end_model", "actor_critic"]
        self.opt = CommonTools(*map(build_opt, self._model_names))
        self.lr_sched = CommonTools(*map(build_lr_sched, self._model_names))

        # Data loaders

        make_data_loader = partial(
            DataLoader,
            dataset=self.train_dataset,
            collate_fn=collate_segments_to_batch,
            num_workers=num_workers,
            persistent_workers=(num_workers > 0),
            pin_memory=self._use_cuda,
            pin_memory_device=str(self._device) if self._use_cuda else "",
        )

        make_batch_sampler = partial(BatchSampler, self.train_dataset, self._rank, self._world_size)

        def get_sample_weights(sample_weights: List[float]) -> Optional[List[float]]:
            return None if (self._is_static_dataset and cfg.static_dataset.ignore_sample_weights) else sample_weights

        c = cfg.denoiser.training
        seq_length = cfg.agent.denoiser.inner_model.num_steps_conditioning + 1 + c.num_autoregressive_steps
        bs = make_batch_sampler(c.batch_size, seq_length, get_sample_weights(c.sample_weights))
        dl_denoiser_train = make_data_loader(batch_sampler=bs)
        dl_denoiser_test = DatasetTraverser(self.test_dataset, c.batch_size, seq_length)

        c = cfg.rew_end_model.training
        bs = make_batch_sampler(c.batch_size, c.seq_length, get_sample_weights(c.sample_weights), can_sample_beyond_end=True)
        dl_rew_end_model_train = make_data_loader(batch_sampler=bs)
        dl_rew_end_model_test = DatasetTraverser(self.test_dataset, c.batch_size, c.seq_length)

        self._data_loader_train = CommonTools(dl_denoiser_train, dl_rew_end_model_train, None)
        self._data_loader_test = CommonTools(dl_denoiser_test, dl_rew_end_model_test, None)

        # RL env

        if self._is_model_free:
            rl_env = make_atari_env(num_envs=cfg.actor_critic.training.batch_size, device=self._device, **cfg.env.train)

        else:
            c = cfg.actor_critic.training
            sl = cfg.agent.denoiser.inner_model.num_steps_conditioning
            bs = make_batch_sampler(c.batch_size, sl, get_sample_weights(c.sample_weights))
            dl_actor_critic = make_data_loader(batch_sampler=bs)
            wm_env_cfg = instantiate(cfg.world_model_env)
            rl_env = WorldModelEnv(self.agent.denoiser, self.agent.rew_end_model, dl_actor_critic, wm_env_cfg)

            if cfg.training.compile_wm:
                rl_env.predict_next_obs = torch.compile(rl_env.predict_next_obs, mode="reduce-overhead")
                rl_env.predict_rew_end = torch.compile(rl_env.predict_rew_end, mode="reduce-overhead")

        # Setup training
        sigma_distribution_cfg = instantiate(cfg.denoiser.sigma_distribution)
        actor_critic_loss_cfg = instantiate(cfg.actor_critic.actor_critic_loss)
        self.agent.setup_training(sigma_distribution_cfg, actor_critic_loss_cfg, rl_env)

        # Training state (things to be saved/restored)
        self.epoch = 0
        self.num_epochs_collect = None
        self.num_episodes_test = 0
        self.num_batch_train = CommonTools(0, 0, 0)
        self.num_batch_test = CommonTools(0, 0, 0)

        if cfg.common.resume:
            self.load_state_checkpoint()
        else:
            self.save_checkpoint()

        if self._rank == 0:
            for name in self._model_names:
                print(f"{count_parameters(getattr(self.agent, name))} parameters in {name}")
            print(self.train_dataset)
            print(self.test_dataset)

        logger.info("Trainer initialized")
        logger.info(f"Device: {self._device}")
        logger.info(f"World size: {self._world_size}")
        logger.info(f"Rank: {self._rank}")

    def run(self) -> None:
        to_log = []

        logger.info("Starting training run")

        if self.epoch == 0:
            if self._is_model_free or self._is_static_dataset:
                self.num_epochs_collect = 0
                logger.info("Model-free or static dataset: skipping initial data collection")
            else:
                if self._rank == 0:
                    logger.info("Collecting initial dataset")
                    self.num_epochs_collect, to_log_ = self.collect_initial_dataset()
                    to_log += to_log_
                self.num_epochs_collect, sd_train_dataset = broadcast_if_needed(self.num_epochs_collect, self.train_dataset.state_dict())
                self.train_dataset.load_state_dict(sd_train_dataset)
                logger.info(f"Initial dataset collection complete. Num epochs collect: {self.num_epochs_collect}")

        num_epochs = self.num_epochs_collect + self._cfg.training.num_final_epochs
        # Maybe hardcode num_epochs 
        num_epochs = 2
        logger.info(f"Total number of epochs: {num_epochs}")

        while self.epoch < num_epochs:
            self.epoch += 1
            start_time = time.time()

            if self._rank == 0:
                print(f"\nEpoch {self.epoch} / {num_epochs}\n")

            logger.info(f"Starting epoch {self.epoch} / {num_epochs}")

            # Training
            should_collect_train = (self._rank == 0 and not self._is_model_free and not self._is_static_dataset and self.epoch <= self.num_epochs_collect)

            if should_collect_train:
                logger.info("Collecting training data")
                c = self._cfg.collection.train
                to_log += self._train_collector.send(NumToCollect(steps=c.steps_per_epoch))
            sd_train_dataset, = broadcast_if_needed(self.train_dataset.state_dict())  # update dataset for ranks > 0
            self.train_dataset.load_state_dict(sd_train_dataset)
            
            if self._cfg.training.should:
                logger.info("Starting agent training")
                to_log += self.train_agent()

            # Evaluation
            should_test = self._rank == 0 and self._cfg.evaluation.should and (self.epoch % self._cfg.evaluation.every == 0)
            should_collect_test = should_test and not self._is_static_dataset

            if should_collect_test:
                logger.info("Collecting test data")
                to_log += self.collect_test()

            if should_test and not self._is_model_free:
                logger.info("Starting agent testing")
                to_log += self.test_agent()

            # Logging
            to_log.append({"duration": (time.time() - start_time) / 3600})
            if self._rank == 0:
                wandb_log(to_log, self.epoch)
                print(f"\nEpoch {self.epoch} logs uploaded. View at: {wandb.run.url}\n")
            to_log = []

            # Checkpointing
            logger.info(f"Saving checkpoint for epoch {self.epoch}")
            self.save_checkpoint()
            
            if dist.is_initialized():
                dist.barrier()

        # Last collect
        if self._rank == 0 and not self._is_static_dataset:
            wandb_log(self.collect_test(final=True), self.epoch)
            print(f"\nFinal test logs uploaded. View at: {wandb.run.url}\n")

        # Final sync
        if self._rank == 0 and wandb.run is not None:
            wandb.finish()

    def collect_initial_dataset(self) -> Tuple[int, Logs]:
        logger.info("Starting initial dataset collection")
        to_log = []
        c = self._cfg.collection.train
        min_steps = c.first_epoch.min
        steps_per_epoch = c.steps_per_epoch
        max_steps = c.first_epoch.max
        threshold_rew = c.first_epoch.threshold_rew
        assert min_steps % steps_per_epoch == 0

        steps = min_steps
        while True:
            to_log += self._train_collector.send(NumToCollect(steps=steps))
            num_steps = self.train_dataset.num_steps
            total_minority_rew = sum(sorted(self.train_dataset.counts_rew)[:-1])
            if total_minority_rew >= threshold_rew:
                break
            if (max_steps is not None) and num_steps >= max_steps:
                print("Reached the specified maximum for initial collect")
                break
            print(f"Minority reward: {total_minority_rew}/{threshold_rew} -> Keep collecting\n")
            steps = steps_per_epoch

        print("\nSummary of initial collect:")
        print(f"Num steps: {num_steps} / {c.num_steps_total}")
        print(f"Reward counts: {dict(self.train_dataset.counter_rew)}")

        remaining_steps = c.num_steps_total - num_steps
        assert remaining_steps % c.steps_per_epoch == 0
        num_epochs_collect = remaining_steps // c.steps_per_epoch

        return num_epochs_collect, to_log

    def collect_test(self, final: bool = False) -> Logs:
        logger.info(f"Starting {'final' if final else 'test'} data collection")
        c = self._cfg.collection.test
        episodes = c.num_final_episodes if final else c.num_episodes
        td = self.test_dataset
        td.clear()
        to_log = self._test_collector.send(NumToCollect(episodes=episodes))
        key_ep_id = f"{td.name}/episode_id"
        to_log = [{k: v + self.num_episodes_test if k == key_ep_id else v for k, v in x.items()} for x in to_log]

        print(f"\nSummary of {'final' if final else 'test'} collect: {td.num_episodes} episodes ({td.num_steps} steps)")
        keys = [key_ep_id, "return", "length"]
        to_log_episodes = [x for x in to_log if set(x.keys()) == set(keys)]
        episode_ids, returns, lengths = [[d[k] for d in to_log_episodes] for k in keys]
        for i, (ep_id, ret, length) in enumerate(zip(episode_ids, returns, lengths)):
            print(f"  Episode {ep_id}: return = {ret} length = {length}\n", end="\n" if i == episodes - 1 else "")

        self.num_episodes_test += episodes

        if final:
            to_log.append({"final_return_mean": np.mean(returns), "final_return_std": np.std(returns)})
            print(to_log[-1])

        return to_log

    def train_agent(self) -> Logs:
        logger.info("Starting agent training")
        self.agent.train()
        self.agent.zero_grad()
        to_log = []
        model_names = ["actor_critic"] if self._is_model_free else self._model_names
        logger.info(f"All onents: {model_names}")
        for name in model_names:
            cfg = getattr(self._cfg, name).training
            if self.epoch > cfg.start_after_epochs:
                steps = cfg.steps_first_epoch if self.epoch == 1 else cfg.steps_per_epoch
                to_log += self.train_component(name, steps)
        return to_log

    @torch.no_grad()
    def test_agent(self) -> Logs:
        logger.info("Starting agent testing")
        self.agent.eval()
        to_log = []
        model_names = [] if self._is_model_free else self._model_names[:-1]
        for name in model_names:
            cfg = getattr(self._cfg, name).training
            if self.epoch > cfg.start_after_epochs:
                to_log += self.test_component(name)
        return to_log

    def train_component(self, name: str, steps: int) -> Logs:
        logger.info(f"Training component: {name}")
        cfg = getattr(self._cfg, name).training
        model = getattr(self._agent, name)
        opt = self.opt.get(name)
        lr_sched = self.lr_sched.get(name)
        data_loader = self._data_loader_train.get(name)

        model.train()
        opt.zero_grad()
        data_iterator = iter(data_loader) if data_loader is not None else None
        to_log = []

        num_steps = cfg.grad_acc_steps * steps
        logger.info(f"total number of steps: {num_steps} - grad_acc_steps: {cfg.grad_acc_steps} - steps: {steps}")
        num_steps = 1000
        logger.info(f"Hardcoded total number of steps: {num_steps}")
        for i in trange(num_steps, desc=f"Training {name}", disable=self._rank > 0):
            batch = next(data_iterator).to(self._device) if data_iterator is not None else None
            loss, metrics = model(batch) if batch is not None else model()
            loss.backward()

            num_batch = self.num_batch_train.get(name)
            metrics[f"num_batch_train_{name}"] = num_batch
            self.num_batch_train.set(name, num_batch + 1)

            if (i + 1) % cfg.grad_acc_steps == 0:
                if cfg.max_grad_norm is not None:
                    grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.max_grad_norm)
                    metrics["grad_norm_before_clip"] = grad_norm

                opt.step()
                opt.zero_grad()

                if lr_sched is not None:
                    metrics["lr"] = lr_sched.get_last_lr()[0]
                    lr_sched.step()

            to_log.append(metrics)

        process_confusion_matrices_if_any_and_compute_classification_metrics(to_log)
        to_log = [{f"{name}/train/{k}": v for k, v in d.items()} for d in to_log]
        return to_log

    @torch.no_grad()
    def test_component(self, name: str) -> Logs:
        model = getattr(self.agent, name)
        data_loader = self._data_loader_test.get(name)
        model.eval()
        to_log = []
        for batch in tqdm(data_loader, desc=f"Evaluating {name}"):
            batch = batch.to(self._device)
            _, metrics = model(batch)
            num_batch = self.num_batch_test.get(name)
            metrics[f"num_batch_test_{name}"] = num_batch
            self.num_batch_test.set(name, num_batch + 1)
            to_log.append(metrics)

        process_confusion_matrices_if_any_and_compute_classification_metrics(to_log)
        to_log = [{f"{name}/test/{k}": v for k, v in d.items()} for d in to_log]
        return to_log

    def load_state_checkpoint(self) -> None:
        self.load_state_dict(torch.load(self._path_state_ckpt, map_location=self._device))

    def save_checkpoint(self) -> None:
        if self._rank == 0:
            save_with_backup(self.state_dict(), self._path_state_ckpt)
            self.train_dataset.save_to_default_path()
            self.test_dataset.save_to_default_path()
            self._keep_agent_copies(self.agent.state_dict(), self.epoch)
            self._save_info_for_import_script(self.epoch)
