''''''
'''
@Author: Huangying Zhan (huangying.zhan.work@gmail.com)
@Date: 2020-05-19
@Copyright: Copyright (C) Huangying Zhan 2020. All rights reserved. Please refer to the license file.
@LastEditTime: 2020-05-28
@LastEditors: Huangying Zhan
@Description: DeepModel initializes different deep networks and provide forward interfaces.
'''

import numpy as np

from .depth.monodepth2.monodepth2 import Monodepth2DepthNet
from .flow.lite_flow_net.lite_flow import LiteFlow
from .pose.monodepth2.monodepth2 import Monodepth2PoseNet

class DeepModel():
    """DeepModel initializes different deep networks and provide forward interfaces.

    TODO:
        add forward_depth()
        
        add forward_pose()

    """
    
    def __init__(self, cfg):
        """
        Args:
            cfg (edict): configuration dictionary
        """
        self.cfg = cfg
        
    def initialize_models(self):
        """intialize multiple deep models
        """

        ''' optical flow '''
        self.flow = self.initialize_deep_flow_model()

        ''' single-view depth '''
        if self.cfg.depth.depth_src is None:
            if self.cfg.depth.pretrained_model is not None:
                self.depth = self.initialize_deep_depth_model()
            else:
                assert False, "No precomputed depths nor pretrained depth model"
        
        ''' two-view pose '''
        if self.cfg.pose_net.enable:
            if self.cfg.pose_net.pretrained_model is not None:
                self.pose = self.initialize_deep_pose_model()
            else:
                assert False, "No pretrained pose model"

    def initialize_deep_flow_model(self):
        """Initialize optical flow network
        
        Returns:
            flow_net (nn.Module): optical flow network
        """
        if self.cfg.deep_flow.network == "liteflow":
            flow_net = LiteFlow(self.cfg.image.height, self.cfg.image.width,
                                self.cfg.deep_flow)
            flow_net.initialize_network_model(
                    weight_path=self.cfg.deep_flow.flow_net_weight
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
        depth_net = Monodepth2DepthNet()
        depth_net.initialize_network_model(
                weight_path=self.cfg.depth.pretrained_model,
                dataset=self.cfg.dataset)
        return depth_net
    
    def initialize_deep_pose_model(self):
        """Initialize two-view pose model

        Returns:
            pose_net (nn.Module): two-view pose network
        """
        pose_net = Monodepth2PoseNet()
        pose_net.initialize_network_model(
            weight_path=self.cfg.pose_net.pretrained_model,
            height=self.cfg.image.height,
            width=self.cfg.image.width,
            dataset=self.cfg.dataset
            )
        return pose_net

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
        cur_imgs = [np.transpose((in_cur_data['img'])/255, (2, 0, 1))]
        ref_imgs = [np.transpose((in_ref_data['img'])/255, (2, 0, 1))]
        ref_imgs = np.asarray(ref_imgs)
        cur_imgs = np.asarray(cur_imgs)

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
        flows[(src_id, tgt_id)] = batch_flows['forward'].copy()[0]
        if forward_backward:
            flows[(tgt_id, src_id)] = batch_flows['backward'].copy()[0]
            flows[(src_id, tgt_id, "diff")] = batch_flows['flow_diff'].copy()[0]
        return flows

    def forward_depth(self):
        """Not implemented
        """
        raise NotImplementedError

    def forward_pose(self):
        """Not implemented
        """
        raise NotImplementedError
