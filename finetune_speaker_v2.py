import os
import json
import argparse
import itertools
import math
import torch
from torch import nn, optim
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.cuda.amp import autocast, GradScaler
from tqdm import tqdm

import librosa
import logging
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
logging.getLogger('numba').setLevel(logging.WARNING)
warnings.filterwarnings(
    "ignore",
    message="stft with return_complex=False is deprecated"
)
from torchaudio._extension.utils import _init_dll_path
_init_dll_path()
import commons
import utils
from data_utils import (
  TextAudioSpeakerLoader,
  TextAudioSpeakerCollate,
  DistributedBucketSampler
)
from models import (
  SynthesizerTrn,
  MultiPeriodDiscriminator,
)
from losses import (
  generator_loss,
  discriminator_loss,
  feature_loss,
  kl_loss
)
from mel_processing import mel_spectrogram_torch, spec_to_mel_torch


torch.backends.cudnn.benchmark = True
global_step = 0


def main():
  """Assume Single Node Multi GPUs Training Only"""
  assert torch.cuda.is_available(), "CPU training is not allowed."

  n_gpus = torch.cuda.device_count()
  os.environ['MASTER_ADDR'] = 'localhost'
  os.environ['MASTER_PORT'] = '8000'
  os.environ['USE_LIBUV'] = '0'
  hps = utils.get_hparams()
  mp.spawn(run, nprocs=n_gpus, args=(n_gpus, hps,))


def run(rank, n_gpus, hps):
  global global_step
  symbols = hps['symbols']
  if rank == 0:
    logger = utils.get_logger(hps.model_dir)
    logger.info(hps)
    utils.check_git_hash(hps.model_dir)
    writer = SummaryWriter(log_dir=hps.model_dir)
    writer_eval = SummaryWriter(log_dir=os.path.join(hps.model_dir, "eval"))

  # Use gloo backend on Windows for Pytorch
  dist.init_process_group(backend=  'gloo' if os.name == 'nt' else 'nccl', init_method='env://', world_size=n_gpus, rank=rank)
  torch.manual_seed(hps.train.seed)
  torch.cuda.set_device(rank)

  train_dataset = TextAudioSpeakerLoader(hps.data.training_files, hps.data, symbols)
  train_sampler = DistributedBucketSampler(
      train_dataset,
      hps.train.batch_size,
      [32,300,400,500,600,700,800,900,1000],
      num_replicas=n_gpus,
      rank=rank,
      shuffle=True)
  collate_fn = TextAudioSpeakerCollate()
  train_loader = DataLoader(train_dataset, num_workers=2, shuffle=False, pin_memory=True,
      collate_fn=collate_fn, batch_sampler=train_sampler)
  # train_loader = DataLoader(train_dataset, batch_size=hps.train.batch_size, num_workers=2, shuffle=False, pin_memory=True,
  #                           collate_fn=collate_fn)
  if rank == 0:
    eval_dataset = TextAudioSpeakerLoader(hps.data.validation_files, hps.data, symbols)
    eval_loader = DataLoader(eval_dataset, num_workers=0, shuffle=False,
        batch_size=hps.train.batch_size, pin_memory=True,
        drop_last=False, collate_fn=collate_fn)

  net_g = SynthesizerTrn(
      len(symbols),
      hps.data.filter_length // 2 + 1,
      hps.train.segment_size // hps.data.hop_length,
      n_speakers=hps.data.n_speakers,
      **hps.model).cuda(rank)
  net_d = MultiPeriodDiscriminator(hps.model.use_spectral_norm).cuda(rank)

  # load existing model
  if hps.cont:
      try:
          _, _, _, epoch_str = utils.load_checkpoint(utils.latest_checkpoint_path(hps.model_dir, "G_latest.pth"), net_g, None)
          _, _, _, epoch_str = utils.load_checkpoint(utils.latest_checkpoint_path(hps.model_dir, "D_latest.pth"), net_d, None)
          global_step = (epoch_str - 1) * len(train_loader)
      except:
          print("Failed to find latest checkpoint, loading G_0.pth...")
          if hps.train_with_pretrained_model:
              print("Train with pretrained model...")
              _, _, _, epoch_str = utils.load_checkpoint("D:\PyCharmWorkSpace\TTS\VITS-fast-fine-tuning\pretrained_models\G_0.pth", net_g, None)
              _, _, _, epoch_str = utils.load_checkpoint("D:\PyCharmWorkSpace\TTS\VITS-fast-fine-tuning\pretrained_models\D_0.pth", net_d, None)
          else:
              print("Train without pretrained model...")
          epoch_str = 1
          global_step = 0
  else:
      if hps.train_with_pretrained_model:
          print("Train with pretrained model...")
          _, _, _, epoch_str = utils.load_checkpoint("D:\PyCharmWorkSpace\TTS\VITS-fast-fine-tuning\pretrained_models\G_0.pth", net_g, None)
          _, _, _, epoch_str = utils.load_checkpoint("D:\PyCharmWorkSpace\TTS\VITS-fast-fine-tuning\pretrained_models\D_0.pth", net_d,None)
      else:
          print("Train without pretrained model...")
      epoch_str = 1
      global_step = 0
  # freeze all other layers except speaker embedding
  for p in net_g.parameters():
      p.requires_grad = True
  for p in net_d.parameters():
      p.requires_grad = True
  # for p in net_d.parameters():
  #     p.requires_grad = False
  # net_g.emb_g.weight.requires_grad = True
  optim_g = torch.optim.AdamW(
      net_g.parameters(),
      hps.train.learning_rate,
      betas=hps.train.betas,
      eps=hps.train.eps)
  optim_d = torch.optim.AdamW(
      net_d.parameters(),
      hps.train.learning_rate,
      betas=hps.train.betas,
      eps=hps.train.eps)
  # optim_d = None
  net_g = DDP(net_g, device_ids=[rank])
  net_d = DDP(net_d, device_ids=[rank])

  scheduler_g = torch.optim.lr_scheduler.ExponentialLR(optim_g, gamma=hps.train.lr_decay)
  scheduler_d = torch.optim.lr_scheduler.ExponentialLR(optim_d, gamma=hps.train.lr_decay)

  scaler = GradScaler(enabled=hps.train.fp16_run)

  for epoch in range(epoch_str, hps.train.epochs + 1):
    if rank==0:
      train_and_evaluate(rank, epoch, hps, [net_g, net_d], [optim_g, optim_d], [scheduler_g, scheduler_d], scaler, [train_loader, eval_loader], logger, [writer, writer_eval])
    else:
      train_and_evaluate(rank, epoch, hps, [net_g, net_d], [optim_g, optim_d], [scheduler_g, scheduler_d], scaler, [train_loader, None], None, None)
    scheduler_g.step()
    scheduler_d.step()


def train_and_evaluate(rank, epoch, hps, nets, optims, schedulers, scaler, loaders, logger, writers):
  net_g, net_d = nets
  optim_g, optim_d = optims
  scheduler_g, scheduler_d = schedulers
  train_loader, eval_loader = loaders
  if writers is not None:
    writer, writer_eval = writers

  # train_loader.batch_sampler.set_epoch(epoch)
  global global_step

  net_g.train()
  net_d.train()
  for batch_idx, (x, x_lengths, spec, spec_lengths, y, y_lengths, speakers) in enumerate(tqdm(train_loader)):
    x, x_lengths = x.cuda(rank, non_blocking=True), x_lengths.cuda(rank, non_blocking=True)
    spec, spec_lengths = spec.cuda(rank, non_blocking=True), spec_lengths.cuda(rank, non_blocking=True)
    y, y_lengths = y.cuda(rank, non_blocking=True), y_lengths.cuda(rank, non_blocking=True)
    speakers = speakers.cuda(rank, non_blocking=True)

    with autocast(enabled=hps.train.fp16_run):
      y_hat, l_length, attn, ids_slice, x_mask, z_mask,\
      (z, z_p, m_p, logs_p, m_q, logs_q) = net_g(x, x_lengths, spec, spec_lengths, speakers)

      mel = spec_to_mel_torch(
          spec,
          hps.data.filter_length,
          hps.data.n_mel_channels,
          hps.data.sampling_rate,
          hps.data.mel_fmin,
          hps.data.mel_fmax)
      y_mel = commons.slice_segments(mel, ids_slice, hps.train.segment_size // hps.data.hop_length)
      y_hat_mel = mel_spectrogram_torch(
          y_hat.squeeze(1),
          hps.data.filter_length,
          hps.data.n_mel_channels,
          hps.data.sampling_rate,
          hps.data.hop_length,
          hps.data.win_length,
          hps.data.mel_fmin,
          hps.data.mel_fmax
      )

      y = commons.slice_segments(y, ids_slice * hps.data.hop_length, hps.train.segment_size) # slice

      # Discriminator
      y_d_hat_r, y_d_hat_g, _, _ = net_d(y, y_hat.detach())
      with autocast(enabled=False):
        loss_disc, losses_disc_r, losses_disc_g = discriminator_loss(y_d_hat_r, y_d_hat_g)
        loss_disc_all = loss_disc
    optim_d.zero_grad()
    scaler.scale(loss_disc_all).backward()
    scaler.unscale_(optim_d)
    grad_norm_d = commons.clip_grad_value_(net_d.parameters(), None)
    scaler.step(optim_d)

    with autocast(enabled=hps.train.fp16_run):
      # Generator
      y_d_hat_r, y_d_hat_g, fmap_r, fmap_g = net_d(y, y_hat)
      with autocast(enabled=False):
        loss_dur = torch.sum(l_length.float())
        loss_mel = F.l1_loss(y_mel, y_hat_mel) * hps.train.c_mel
        loss_kl = kl_loss(z_p, logs_q, m_p, logs_p, z_mask) * hps.train.c_kl

        loss_fm = feature_loss(fmap_r, fmap_g)
        loss_gen, losses_gen = generator_loss(y_d_hat_g)
        loss_gen_all = loss_gen + loss_fm + loss_mel + loss_dur + loss_kl
    optim_g.zero_grad()
    scaler.scale(loss_gen_all).backward()
    scaler.unscale_(optim_g)
    grad_norm_g = commons.clip_grad_value_(net_g.parameters(), None)
    scaler.step(optim_g)
    scaler.update()

    if rank==0:
      if global_step % hps.train.log_interval == 0:
        lr = optim_g.param_groups[0]['lr']
        losses = [loss_disc, loss_gen, loss_fm, loss_mel, loss_dur, loss_kl]
        logger.info('Train Epoch: {} [{:.0f}%]'.format(
          epoch,
          100. * batch_idx / len(train_loader)))
        logger.info([x.item() for x in losses] + [global_step, lr])

        scalar_dict = {"loss/g/total": loss_gen_all, "loss/d/total": loss_disc_all, "learning_rate": lr, "grad_norm_g": grad_norm_g}
        scalar_dict.update({"loss/g/fm": loss_fm, "loss/g/mel": loss_mel, "loss/g/dur": loss_dur, "loss/g/kl": loss_kl})

        scalar_dict.update({"loss/g/{}".format(i): v for i, v in enumerate(losses_gen)})
        scalar_dict.update({"loss/d_r/{}".format(i): v for i, v in enumerate(losses_disc_r)})
        scalar_dict.update({"loss/d_g/{}".format(i): v for i, v in enumerate(losses_disc_g)})
        image_dict = {
            "slice/mel_org": utils.plot_spectrogram_to_numpy(y_mel[0].data.cpu().numpy()),
            "slice/mel_gen": utils.plot_spectrogram_to_numpy(y_hat_mel[0].data.cpu().numpy()),
            "all/mel": utils.plot_spectrogram_to_numpy(mel[0].data.cpu().numpy()),
            "all/attn": utils.plot_alignment_to_numpy(attn[0,0].data.cpu().numpy())
        }
        utils.summarize(
          writer=writer,
          global_step=global_step,
          images=image_dict,
          scalars=scalar_dict)

      if global_step % hps.train.eval_interval == 0:
        evaluate(hps, net_g, eval_loader, writer_eval)
        
        utils.save_checkpoint(net_g, None, hps.train.learning_rate, epoch,
                              os.path.join(hps.model_dir, "G_latest.pth"))
        
        utils.save_checkpoint(net_d, None, hps.train.learning_rate, epoch,
                              os.path.join(hps.model_dir, "D_latest.pth"))
        # save to google drive
        if os.path.exists("D:\\PyCharmWorkSpace\\TTS\\VITS-fast-fine-tuning\\output\\"):
            utils.save_checkpoint(net_g, None, hps.train.learning_rate, epoch,
                                  os.path.join("D:\\PyCharmWorkSpace\\TTS\\VITS-fast-fine-tuning\\output\\", "G_latest.pth"))

            utils.save_checkpoint(net_d, None, hps.train.learning_rate, epoch,
                                  os.path.join("D:\\PyCharmWorkSpace\\TTS\\VITS-fast-fine-tuning\\output\\", "D_latest.pth"))
        if hps.preserved > 0:
          utils.save_checkpoint(net_g, None, hps.train.learning_rate, epoch,
                                  os.path.join(hps.model_dir, "G_{}.pth".format(global_step)))
          utils.save_checkpoint(net_d, None, hps.train.learning_rate, epoch,
                                  os.path.join(hps.model_dir, "D_{}.pth".format(global_step)))
          old_g = utils.oldest_checkpoint_path(hps.model_dir, "G_[0-9]*.pth",
                                               preserved=hps.preserved)  # Preserve 4 (default) historical checkpoints.
          old_d = utils.oldest_checkpoint_path(hps.model_dir, "D_[0-9]*.pth", preserved=hps.preserved)
          if os.path.exists(old_g):
            print(f"remove {old_g}")
            os.remove(old_g)
          if os.path.exists(old_d):
            print(f"remove {old_d}")
            os.remove(old_d)
          if os.path.exists("D:\\PyCharmWorkSpace\\TTS\\VITS-fast-fine-tuning\\output\\"):
              utils.save_checkpoint(net_g, None, hps.train.learning_rate, epoch,
                                    os.path.join("D:\\PyCharmWorkSpace\\TTS\\VITS-fast-fine-tuning\\output\\", "G_{}.pth".format(global_step)))
              utils.save_checkpoint(net_d, None, hps.train.learning_rate, epoch,
                                    os.path.join("D:\\PyCharmWorkSpace\\TTS\\VITS-fast-fine-tuning\\output\\", "D_{}.pth".format(global_step)))
              old_g = utils.oldest_checkpoint_path("D:\\PyCharmWorkSpace\\TTS\\VITS-fast-fine-tuning\\output\\", "G_[0-9]*.pth",
                                                   preserved=hps.preserved)  # Preserve 4 (default) historical checkpoints.
              old_d = utils.oldest_checkpoint_path("D:\\PyCharmWorkSpace\\TTS\\VITS-fast-fine-tuning\\output\\", "D_[0-9]*.pth", preserved=hps.preserved)
              if os.path.exists(old_g):
                  print(f"remove {old_g}")
                  os.remove(old_g)
              if os.path.exists(old_d):
                  print(f"remove {old_d}")
                  os.remove(old_d)
    global_step += 1
    if epoch > hps.max_epochs:
        print("Maximum epoch reached, closing training...")
        exit()

  # if rank == 0:
  #   logger.info('====> Epoch: {}'.format(epoch))


def evaluate(hps, generator, eval_loader, writer_eval):
    generator.eval()
    with torch.no_grad():
      for batch_idx, (x, x_lengths, spec, spec_lengths, y, y_lengths, speakers) in enumerate(eval_loader):
        x, x_lengths = x.cuda(0), x_lengths.cuda(0)
        spec, spec_lengths = spec.cuda(0), spec_lengths.cuda(0)
        y, y_lengths = y.cuda(0), y_lengths.cuda(0)
        speakers = speakers.cuda(0)

        # remove else
        x = x[:1]
        x_lengths = x_lengths[:1]
        spec = spec[:1]
        spec_lengths = spec_lengths[:1]
        y = y[:1]
        y_lengths = y_lengths[:1]
        speakers = speakers[:1]
        break
      y_hat, attn, mask, *_ = generator.module.infer(x, x_lengths, speakers, max_len=1000)
      y_hat_lengths = mask.sum([1,2]).long() * hps.data.hop_length

      mel = spec_to_mel_torch(
        spec,
        hps.data.filter_length,
        hps.data.n_mel_channels,
        hps.data.sampling_rate,
        hps.data.mel_fmin,
        hps.data.mel_fmax)
      y_hat_mel = mel_spectrogram_torch(
        y_hat.squeeze(1).float(),
        hps.data.filter_length,
        hps.data.n_mel_channels,
        hps.data.sampling_rate,
        hps.data.hop_length,
        hps.data.win_length,
        hps.data.mel_fmin,
        hps.data.mel_fmax
      )
    image_dict = {
      "gen/mel": utils.plot_spectrogram_to_numpy(y_hat_mel[0].cpu().numpy())
    }
    audio_dict = {
      "gen/audio": y_hat[0,:,:y_hat_lengths[0]]
    }
    if global_step == 0:
      image_dict.update({"gt/mel": utils.plot_spectrogram_to_numpy(mel[0].cpu().numpy())})
      audio_dict.update({"gt/audio": y[0,:,:y_lengths[0]]})

    utils.summarize(
      writer=writer_eval,
      global_step=global_step,
      images=image_dict,
      audios=audio_dict,
      audio_sampling_rate=hps.data.sampling_rate
    )
    generator.train()


if __name__ == "__main__":
  main()
