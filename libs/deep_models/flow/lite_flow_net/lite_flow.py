''''''
'''
@Author: Huangying Zhan (huangying.zhan.work@gmail.com)
@Date: 2020-05-19
@Copyright: Copyright (C) Huangying Zhan 2020. All rights reserved. Please refer to the license file.
@LastEditTime: 2020-05-26
@LastEditors: Huangying Zhan
@Description: This is the interface for LiteFlowNet
'''

import cv2
import math
import numpy as np
import os
import sys
import torch
import torch.nn.functional as F
import torch.optim as optim

from libs.general.utils import image_grid
from libs.deep_models.depth.monodepth2.layers import FlowToPix, PixToFlow, SSIM, get_smooth_loss

from .lite_flow_net import LiteFlowNet
from ..deep_flow import DeepFlow


class LiteFlow(DeepFlow):
    """LiteFlow is the interface for LiteFlowNet. It allows (optional) online finetuning.
    """

    def __init__(self, *args, **kwargs):
        super(LiteFlow, self).__init__(*args, **kwargs)
        
        
        # FIXME: half-flow issue
        self.half_flow = False

        # Online finetuning configuration
        self.finetune = self.flow_cfg.online_finetune.enable
    
    def setup_train(self):
        """Setup training configurations for online finetuning
        """
        # Basic configuration
        self.batch_size = 1
        self.img_cnt = 0
        self.device = torch.device('cuda')
        self.flow_scales = [1]
        self.num_flow_scale = len(self.flow_scales)
        self.frame_ids = [0, 1]

        # Layer setup
        self.flow_to_pix = FlowToPix(self.batch_size, self.height, self.width) 
        self.flow_to_pix.to(self.device)
        self.ssim = SSIM()
        self.ssim.to(self.device)
        
        # Optimization
        self.learning_rate = self.flow_cfg.online_finetune.lr
        self.parameters_to_train = []
        self.parameters_to_train += list(self.model.parameters())
        self.model_optimizer = optim.Adam(self.parameters_to_train, self.learning_rate)

        # loss
        self.flow_forward_backward = True
        self.flow_consistency = self.flow_cfg.online_finetune.loss.flow_consistency
        self.flow_smoothness = self.flow_cfg.online_finetune.loss.flow_smoothness

    def initialize_network_model(self, weight_path):
        """initialize flow_net model with weight_path
        
        Args:
            weight_path (str): weight path
        """
        if weight_path is not None:
            print("==> Initialize LiteFlowNet with [{}]: ".format(weight_path))
            # Initialize network
            self.model = LiteFlowNet().cuda()

            # Load model weights
            checkpoint = torch.load(weight_path)
            self.model.load_state_dict(checkpoint)

            if self.finetune:
                self.model.train()
                self.setup_train()
            else:
                self.model.eval()
        else:
            assert False, "No LiteFlowNet pretrained model is provided."

    @torch.no_grad()
    def inference_no_grad(self, img1, img2):
        """Predict optical flow for the given pairs (no grad)
        
        Args:
            img1 (array, [Nx3xHxW]): image 1; intensity [0-1]
            img2 (array, [Nx3xHxW]): image 2; intensity [0-1]
        
        Returns:
            flow (tensor, [Nx2xHxW]): flow from img1 to img2
        """
        return self.inference(img1, img2)

    def inference(self, img1, img2):
        """Predict optical flow for the given pairs
        
        Args:
            img1 (array, [Nx3xHxW]): image 1; intensity [0-1]
            img2 (array, [Nx3xHxW]): image 2; intensity [0-1]
        
        Returns:
            flow (tensor, [Nx2xHxW]): flow from img1 to img2
        """
        # Convert to torch array:cuda
        img1 = torch.from_numpy(img1).float().cuda()
        img2 = torch.from_numpy(img2).float().cuda()

        _, _, h, w = img1.shape
        th, tw = self.get_target_size(h, w)

        # forward pass
        flow_inputs = [img1, img2]
        resized_img_list = [
                            F.interpolate(
                                img, (th, tw), mode='bilinear', align_corners=True)
                            for img in flow_inputs
                        ]
        output = self.model(resized_img_list)

        # Post-process output
        flow = self.resize_dense_flow(
                                output[1],
                                h, w)
        if self.half_flow:
            flow /= 2.
        return flow
    
    def train_flow(self, combined_flow_data, flow_diff, img1, img2):
        """Train flow model using 
        - photometric loss
        - flow smoothness loss
        - flow consistency loss

        Args:
            combined_flow_data (tensor, [Nx2xHxW]): forward and backward flow
            flow_diff (tensor, [1xHxWx1]): forward-backward flow differnce
            img1 (array, [1x3xHxW]): image 1
            img2 (array, [1x3xHxW]): image 2
        """
        losses = {'loss': 0}
        inputs = {
            ('color', 0, 0): torch.from_numpy(img1).float().cuda(),
            ('color', 1, 0): torch.from_numpy(img2).float().cuda(),
        }
        outputs = {
            ('flow', 0, 1, 1):  combined_flow_data[0:1],
            ('flow', 1, 0, 1):  combined_flow_data[1:2],
            ('flow_diff', 0, 1, 1):  combined_flow_data[0:1],
        }
        self.generate_images_pred_flow(inputs, outputs)

        losses.update(self.compute_flow_losses(inputs, outputs))
        losses["loss"] += losses["flow_loss"]

        self.model_optimizer.zero_grad()
        losses["loss"].backward()
        self.model_optimizer.step()

    def generate_images_pred_flow(self, inputs, outputs):
        """Generate the warped (reprojected) color images using optical flow for a minibatch.
        Generated images are saved into the `outputs` dictionary.
        """
        source_scale = 0
        for i, f_i in enumerate(self.frame_ids[1:]):
            if f_i != "s":
                for scale in self.flow_scales:
                    # Warp image using forward flow
                    flow = outputs[("flow", 0, f_i, scale)]
                    flow = self.resize_dense_flow(flow, self.height, self.width)
                    pix_coords = self.flow_to_pix(flow)
                    outputs[("sample_flow", 0, f_i, scale)] = pix_coords
                    outputs[("color_flow", 0, f_i, scale)] = F.grid_sample(
                        inputs[("color", f_i, source_scale)],
                        pix_coords,
                        padding_mode="border")
                    
                    if self.flow_forward_backward:
                        # Warp image using backward flow
                        flow = outputs[("flow", f_i, 0, scale)]
                        flow = self.resize_dense_flow(flow, self.height, self.width)
                        pix_coords = self.flow_to_pix(flow)
                        outputs[("sample_flow", f_i, 0, scale)] = pix_coords
                        outputs[("color_flow", f_i, 0, scale)] = F.grid_sample(
                            inputs[("color", 0, source_scale)],
                            pix_coords,
                            padding_mode="border")
    
    def compute_flow_losses(self, inputs, outputs):
        """Compute the reprojection and smoothness losses for a minibatch
        if forward_backward consistency is enabled:
            warp_flow and flow_diff are saved
        """
        losses = {}
        total_loss = 0

        source_scale = 0
        for scale in self.flow_scales:
            loss = 0
            reprojection_losses = []

            """ reprojection loss """
            for frame_id in self.frame_ids[1:]:
                if frame_id != "s":
                    target = inputs[("color", 0, source_scale)]
                    pred = outputs[("color_flow", 0, frame_id, scale)]
                    reprojection_losses.append(self.compute_reprojection_loss(pred, target))

            reprojection_losses = torch.cat(reprojection_losses, 1)

            combined = reprojection_losses

            if combined.shape[1] == 1:
                to_optimise = combined
            else:
                to_optimise, idxs = torch.min(combined, dim=1)

            loss += to_optimise.mean()

            """ flow smoothness loss """
            for frame_id in self.frame_ids[1:]:
                if frame_id != "s":
                    flow = outputs[("flow", 0, frame_id, scale)].norm(dim=1, keepdim=True)
                    color = inputs[("color", 0, source_scale)]
                    mean_flow = flow.mean(2, True).mean(3, True)
                    norm_flow = flow / (mean_flow + 1e-7)
                    smooth_loss = get_smooth_loss(norm_flow, color)
                    loss += self.flow_smoothness * smooth_loss / (2 ** scale)
                    
                    if self.flow_forward_backward:
                        flow = outputs[("flow", frame_id, 0, scale)].norm(dim=1, keepdim=True)
                        color = inputs[("color", frame_id, source_scale)]
                        mean_flow = flow.mean(2, True).mean(3, True)
                        norm_flow = flow / (mean_flow + 1e-7)
                        smooth_loss = get_smooth_loss(norm_flow, color)
                        loss += self.flow_smoothness * smooth_loss / (2 ** scale)

            """ flow forward-backward consistency loss """
            # if self.flow_forward_backward:
            for frame_id in self.frame_ids[1:]:
                if frame_id != "s":
                    flow_consistency_loss = outputs[("flow_diff", 0, frame_id, scale)].mean()
                    loss += self.flow_consistency * flow_consistency_loss / (2 ** scale)
            total_loss += loss
            losses["flow_loss/{}".format(scale)] = loss

        total_loss /= self.num_flow_scale
        losses["flow_loss"] = total_loss
        return losses

    def compute_reprojection_loss(self, pred, target):
        """Computes reprojection loss between a batch of predicted and target images
        """
        abs_diff = torch.abs(target - pred)
        l1_loss = abs_diff.mean(1, True)

        
        ssim_loss = self.ssim(pred, target).mean(1, True)
        reprojection_loss = 0.85 * ssim_loss + 0.15 * l1_loss

        return reprojection_loss

    def inference_flow(self, 
                    img1, img2,
                    flow_dir,
                    forward_backward=False,
                    dataset='kitti'):
        """Estimate flow (1->2) and form keypoints
        
        Args:
            img1 (array [Nx3xHxW]): image 1
            img2 (array [Nx3xHxW]): image 2
            flow_dir (str): if directory is given, img1 and img2 become list of img ids
            foward_backward (bool): forward-backward flow consistency is used if True
            dataset (str): dataset type
        
        Returns:
            a dictionary containing
                - **forward** (array [Nx2xHxW]) : foward flow
                - **backward** (array [Nx2xHxW]) : backward flow
                - **flow_diff** (array [NxHxWx1]) : foward-backward flow inconsistency
        """
        # flow net inference to get flows
        if forward_backward:
            input_img1 = np.concatenate([img1, img2], axis=0)
            input_img2 = np.concatenate([img2, img1], axis=0)
        else:
            input_img1 = img1
            input_img2 = img2
        
        # inference with/without gradient
        if self.finetune:
            combined_flow_data = self.inference(input_img1, input_img2)
        else:
            combined_flow_data = self.inference_no_grad(input_img1, input_img2)
        flow_data = combined_flow_data[0:1]
        if forward_backward:
            back_flow_data = combined_flow_data[1:2]

        # sampled flow
        # Get sampling pixel coordinates
        px1on2 = self.flow_to_pix(flow_data)

        # Forward-Backward flow consistency check
        if forward_backward:
            # get flow-consistency error map
            flow_diff = self.forward_backward_consistency(
                                flow1=flow_data,
                                flow2=back_flow_data,
                                px1on2=px1on2)
        
        # online finetune
        if self.finetune and self.img_cnt < self.flow_cfg.online_finetune.num_frames:
            self.train_flow(combined_flow_data, flow_diff, img1, img2)
            self.img_cnt += 1
        
        # summarize flow data and flow difference
        flows = {}
        flows['forward'] = flow_data.detach().cpu().numpy()
        if forward_backward:
            flows['backward'] = back_flow_data.detach().cpu().numpy()
            flows['flow_diff'] = flow_diff.detach().cpu().numpy()
        return flows
    
    def forward_backward_consistency(self, flow1, flow2, px1on2):
        """Compute flow consistency map

        Args:
            flow1 (tensor, [Nx2xHxW]): flow map 1
            flow2 (tensor, [Nx2xHxW]): flow map 2
            px1on2 (tensor, [NxHxWx2]): projected pixel of view 1 on view 2
        
        Returns:
            flow_diff (array, [NxHxWx1]): flow inconsistency error map
        """
        # Warp flow2 to flow1
        warp_flow1 = F.grid_sample(-flow2, px1on2)

        # Calculate flow difference
        flow_diff = (flow1 - warp_flow1)

        # TODO: UnFlow (Meister etal. 2017) constrain is not used
        UnFlow_constrain = False
        if UnFlow_constrain:
            flow_diff = (flow_diff ** 2 - 0.01 * (flow1**2 - warp_flow1 ** 2))

        # copy flow_diff to cpu
        flow_diff = flow_diff.norm(dim=1, keepdim=True)
        flow_diff = flow_diff.permute(0, 2, 3, 1)
        return flow_diff
