''''''
'''
@Author: Huangying Zhan (huangying.zhan.work@gmail.com)
@Date: 2020-05-19
@Copyright: Copyright (C) Huangying Zhan 2020. All rights reserved. Please refer to the license file.
@LastEditTime: 2020-06-03
@LastEditors: Huangying Zhan
@Description: DeepModel initializes different deep networks and provide forward interfaces.
'''

import numpy as np
import PIL.Image as pil
import torch
import torch.optim as optim
from torchvision import transforms

from .depth.monodepth2.monodepth2 import Monodepth2DepthNet
from .flow.lite_flow_net.lite_flow import LiteFlow
from .pose.monodepth2.monodepth2 import Monodepth2PoseNet
from libs.deep_models.depth.monodepth2.layers import FlowToPix, PixToFlow, SSIM, get_smooth_loss

class DeepModel():
    """DeepModel initializes different deep networks and provide forward interfaces.
    """
    
    def __init__(self, cfg):
        """
        Args:
            cfg (edict): configuration dictionary
        """
        self.cfg = cfg
        self.finetune_cfg = self.cfg.online_finetune
        self.device = torch.device('cuda')
        
    def initialize_models(self):
        """intialize multiple deep models
        """

        ''' optical flow '''
        self.flow = self.initialize_deep_flow_model()

        ''' single-view depth '''
        if self.cfg.depth.depth_src is None:
            if self.cfg.depth.deep_depth.pretrained_model is not None:
                self.depth = self.initialize_deep_depth_model()
            else:
                assert False, "No precomputed depths nor pretrained depth model"
        
        ''' two-view pose '''
        if self.cfg.deep_pose.enable:
            if self.cfg.deep_pose.pretrained_model is not None:
                self.pose = self.initialize_deep_pose_model()
            else:
                assert False, "No pretrained pose model"

    def initialize_deep_flow_model(self):
        """Initialize optical flow network
        
        Returns:
            flow_net (nn.Module): optical flow network
        """
        if self.cfg.deep_flow.network == 'liteflow':
            flow_net = LiteFlow(self.cfg.image.height, self.cfg.image.width)
            flow_net.initialize_network_model(
                    weight_path=self.cfg.deep_flow.flow_net_weight,
                    finetune=self.finetune_cfg.flow.enable
                    )
        else:
            assert False, "Invalid flow network [{}] is provided.".format(
                                self.cfg.deep_flow.network
                                )
        return flow_net

    def initialize_deep_depth_model(self):
        """Initialize single-view depth model

        Returns:
            depth_net (nn.Module): single-view depth network
        """
        if self.cfg.depth.deep_depth.network == 'monodepth2':
            depth_net = Monodepth2DepthNet(self.cfg.depth.deep_depth)
            depth_net.initialize_network_model(
                    weight_path=self.cfg.depth.deep_depth.pretrained_model,
                    dataset=self.cfg.dataset)
        else:
            assert False, "Invalid depth network [{}] is provided.".format(
                                self.cfg.depth.deep_depth.network
                                )
        return depth_net
    
    def initialize_deep_pose_model(self):
        """Initialize two-view pose model

        Returns:
            pose_net (nn.Module): two-view pose network
        """
        pose_net = Monodepth2PoseNet(self.cfg.deep_pose)
        pose_net.initialize_network_model(
            weight_path=self.cfg.deep_pose.pretrained_model,
            height=self.cfg.image.height,
            width=self.cfg.image.width,
            dataset=self.cfg.dataset
            )
        return pose_net

    def setup_train(self):
        """Setup training configurations for online finetuning
        """
        # Basic configuration
        self.img_cnt = 0
        self.frame_ids = [0, 1]

        # Optimization
        self.learning_rate = self.finetune_cfg.lr
        self.parameters_to_train = []

        # flow train setup
        if self.finetune_cfg.flow.enable:
            self.flow.setup_train(self, self.finetune_cfg.flow)
        
        # # depth train setup
        # if self.finetune_cfg.depth.enable:
        #     self.setup_train_depth()
        
        # # depth train setup
        # if self.finetune_cfg.depth.enable:
        #     self.setup_train_depth()
        
        self.model_optimizer = optim.Adam(self.parameters_to_train, self.learning_rate)


    def forward_flow(self, in_cur_data, in_ref_data, forward_backward):
        """Optical flow network forward interface, a forward inference.

        Args:
            in_cur_data (dict): current data
            in_ref_data (dict): reference data
            forward_backward (bool): use forward-backward consistency if True
        
        Returns:
            flows (dict): predicted flow data. flows[(id1, id2)] is flows from id1 to id2.

                - **flows(id1, id2)** (array, 2xHxW): flows from id1 to id2
                - **flows(id2, id1)** (array, 2xHxW): flows from id2 to id1
                - **flows(id1, id2, 'diff)** (array, 1xHxW): flow difference of id1
        """
        # Preprocess image
        cur_imgs = np.transpose((in_cur_data['img'])/255, (2, 0, 1))
        ref_imgs = np.transpose((in_ref_data['img'])/255, (2, 0, 1))
        cur_imgs = torch.from_numpy(cur_imgs).unsqueeze(0).float().cuda()
        ref_imgs = torch.from_numpy(ref_imgs).unsqueeze(0).float().cuda()

        # Forward pass
        flows = {}

        # Flow inference
        batch_flows = self.flow.inference_flow(
                                img1=ref_imgs,
                                img2=cur_imgs,
                                forward_backward=forward_backward,
                                dataset=self.cfg.dataset)
        
        # Save flows at current view
        src_id = in_ref_data['id']
        tgt_id = in_cur_data['id']
        flows[(src_id, tgt_id)] = batch_flows['forward'].detach().cpu().numpy()[0]
        if forward_backward:
            flows[(tgt_id, src_id)] = batch_flows['backward'].detach().cpu().numpy()[0]
            flows[(src_id, tgt_id, "diff")] = batch_flows['flow_diff'].detach().cpu().numpy()[0]
        return flows

    def forward_depth(self, imgs):
        """Depth network forward interface, a forward inference.

        Args:
            imgs (list): list of images, each element is a [HxWx3] array

        Returns:
            depth (array, [HxW]): depth map of imgs[0]
        """
        # Preprocess
        img_tensor = []
        for img in imgs:
            input_image = pil.fromarray(img)
            input_image = input_image.resize((self.depth.feed_width, self.depth.feed_height), pil.LANCZOS)
            input_image = transforms.ToTensor()(input_image).unsqueeze(0)
            img_tensor.append(input_image)
        img_tensor = torch.cat(img_tensor, 0)
        img_tensor = img_tensor.cuda()
        
        # Inference
        if self.depth.finetune:
            pred_depths = self.depth.inference(img_tensor)
        else:
            pred_depths = self.depth.inference_no_grad(img_tensor)
        
        self.pred_depths = pred_depths
        depth = pred_depths[0].detach().cpu().numpy()[0,0]
        return depth

    def forward_pose(self, imgs):
        """Depth network forward interface, a forward inference.

        Args:
            imgs (list): list of images, each element is a [HxWx3] array

        Returns:
            pose (array, [4x4]): relative pose from img2 to img1
        """
        # Preprocess
        img_tensor = []
        for img in imgs:
            input_image = pil.fromarray(img)
            input_image = input_image.resize((self.depth.feed_width, self.depth.feed_height), pil.LANCZOS)
            input_image = transforms.ToTensor()(input_image).unsqueeze(0)
            img_tensor.append(input_image)
        img_tensor = torch.cat(img_tensor, 1)
        img_tensor = img_tensor.cuda()

        # Prediction
        if self.pose.finetune:
            pred_poses = self.pose.inference(img_tensor)
        else:
            pred_poses = self.pose.inference_no_grad(img_tensor)
        
        self.pred_poses = pred_poses
        pose = pred_poses.detach().cpu().numpy()[0]
        return pose

    def finetune(self, img1, img2):
        """Finetuning deep models

        Args:
            img1 (array, [HxWx3]): image 1 (reference)
            img2 (array, [HxWx3]): image 2 (current)
        """
        img1 = np.transpose((img1)/255, (2, 0, 1))
        img2 = np.transpose((img2)/255, (2, 0, 1))
        img1 = torch.from_numpy(img1).unsqueeze(0).float().cuda()
        img2 = torch.from_numpy(img2).unsqueeze(0).float().cuda()

        if self.finetune_cfg.num_frames is None or self.img_cnt < self.finetune_cfg.num_frames:
            ''' data preparation '''
            losses = {'loss': 0}
            inputs = {
                ('color', 0, 0): img1,
                ('color', 1, 0): img2,
            }
            outputs = {}

            ''' loss computation '''
            # flow
            if self.finetune_cfg.flow.enable:
                assert self.cfg.deep_flow.forward_backward, "forward-backward option has to be True for finetuning"
                for s in self.flow.flow_scales:
                    outputs.update(
                        {('flow', 0, 1, s):  self.flow.forward_flow[s],
                        ('flow', 1, 0, s):  self.flow.backward_flow[s],
                        ('flow_diff', 0, 1, s):  self.flow.flow_diff[s]}
                    )

                losses.update(self.flow.train(inputs, outputs))
                losses["loss"] += losses["flow_loss"]
            
            ''' backward '''
            self.model_optimizer.zero_grad()
            losses["loss"].backward()
            self.model_optimizer.step()
            
            self.img_cnt += 1

        else:
            # reset flow model to eval mode
            if self.finetune_cfg.flow.enable:
                self.flow.model.eval()
            
            # # reset depth model to eval mode
            # if self.finetune_cfg.depth.enable:
            #     self.depth.model.eval()
            
            # # reset pose model to eval mode
            # if self.finetune_cfg.pose.enable:
            #     self.pose.model.eval()
