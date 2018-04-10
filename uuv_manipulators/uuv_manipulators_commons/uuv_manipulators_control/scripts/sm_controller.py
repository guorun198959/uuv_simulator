#!/usr/bin/env python
# Copyright (c) 2016 The UUV Simulator Authors.
# All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import rospy
import sys
import os
import numpy as np
from std_msgs.msg import Float32MultiArray
from sensor_msgs.msg import JointState
from uuv_gazebo_ros_plugins_msgs.srv import GetModelProperties
#from urdf_parser_py.urdf import URDF
#from kdl.kdl_parser import kdl_tree_from_urdf_model

import PyKDL
from uuv_manipulators_control import CartesianController


class SMC(CartesianController):
    """
    Joint space Sliding Mode controller
    """

    LABEL = 'Joint space Sliding Mode controller'
    def __init__(self):
        """
        Class constructor
        """
        CartesianController.__init__(self)
        # Retrieve the controller parameters from the parameter server
        Q_tag = '~Q'
        K_tag = '~K'
        uuv_name_tag = '~uuv_name'
        if not rospy.has_param(Q_tag):
            rospy.ROSException('Q gain matrix not available for tag=%s' % Q_tag)
        if not rospy.has_param(K_tag):
            rospy.ROSException('K gain matrix not available for tag=%s' % K_tag)
        if not rospy.has_param(uuv_name_tag):
            rospy.ROSException('K gain matrix not available for tag=%s' % uuv_name_tag)

        self._last_joint_goal = np.matrix([0, 0.5*np.pi, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]).T

        # Current joint position and velocity states
        self._joint_state = np.matrix([np.zeros(12)]).T

        # Last velocity reference in joint coordinates
        self._last_qdot_cmd = np.asmatrix(np.zeros(6)).T

        # Initialization of Sliding Variables
        self._I = np.zeros((6,6))
        np.fill_diagonal(self._I, np.ones(6))
        self._I = np.asmatrix(self._I)

        self._lambda = self._I
        np.fill_diagonal(self._lambda, 1 * np.ones(6))

        self._T = self._I
        t = [.2, .2, .2, .2, .2, .2]
        np.fill_diagonal(self._T, np.divide(1.0, t) * np.ones(6))

        self._Q = self._I
        self._q = rospy.get_param(Q_tag)
        np.fill_diagonal(self._Q, self._q * np.ones(6))

        self._K = self._I
        self._k = rospy.get_param(K_tag)
        np.fill_diagonal(self._K, self._k * np.ones(6))

        self._uuv_name = rospy.get_param(uuv_name_tag)

        self._last_time = rospy.get_time()

        # Initialization flag, to wait the end-effector get to the home position
        self._is_init = False

        self._update_model_props()

        # Subscriber to the joint states
        self._joint_sub = rospy.Subscriber("/"+self._uuv_name+"/joint_states", JointState, self._joint_callback)

        self._run()

    def _update(self):
        # Leave if ROS is not running or command is not valid
        if rospy.is_shutdown() or self._last_goal is None:
            return

        # Calculate the goal pose
        goal = self._get_goal()
        # Calculate velocity reference in joint coordinates
        time_step = rospy.get_time() - self._last_time
        self._last_time = rospy.get_time()
        goal_p_dot = (goal.p - self._last_goal.p) / time_step
        goal_vel = np.array([goal_p_dot[0], goal_p_dot[1], goal_p_dot[2], 0, 0, 0])
        J_inv = self._arm_interface.jacobian_pseudo_inverse()
        qdot_cmd = J_inv * np.asmatrix(goal_vel).T
        # Calculate the joints goal
        joint_goal = np.matrix(np.append(self._arm_interface.inverse_kinematics(goal.p, goal.M.GetQuaternion()), qdot_cmd)).T
        if joint_goal.item(0) is None:
            joint_goal = self._last_joint_goal
        else:
            self._last_joint_goal = joint_goal
        # Calculate acceleration reference in joint coordinates
        qddot_cmd = (joint_goal[6:] - self._last_qdot_cmd) / time_step
        self._last_qdot_cmd = joint_goal[6:]
        # Calculate the joints error
        joint_error = joint_goal - self._joint_state
        # Calculate sliding Variable
        s = self._lambda * joint_error[:6] + joint_error[6:]
        # Calculate inertia matrix
        M_q = 0
        for key in self._linkloads:
            M_q += self._arm_interface.jacobian_transpose(end_link=key) * self._linkinertias[key] * self._arm_interface.jacobian(end_link=key)

        ########################################################################
        ########################################################################

        # End-effector's pose
        ee_pose = self._arm_interface.get_ee_pose_as_frame()
        # End-effector's velocity
        ee_vel = self._arm_interface.get_ee_vel_as_kdl_twist()
        # Calculate pose error
        error = PyKDL.diff(goal, ee_pose)
        # End-effector wrench to achieve target
        wrench = np.matrix(np.zeros(6)).T
        for i in range(len(wrench)):
            wrench[i] = -(10000 * error[i] + 0 * ee_vel[i])
            #wrench[i] = -(self._Kp[i] * error[i] + self._Kd[i] * ee_vel[i])

        # Compute jacobian transpose
        JT = self._arm_interface.jacobian_transpose()
        # Calculate the torques for the joints
        tau = JT * wrench
        # Store current pose target
        self._last_goal = goal

        self.publish_goal()
        self.publish_joint_efforts(tau)

    # Get joint state of manipulator arm from azimuth to wrist
    def _joint_callback(self, joint_state):
        self._joint_state = np.matrix([joint_state.position[1:7] + joint_state.velocity[1:7]]).T

    # Update model properties
    def _update_model_props(self):
        rospy.wait_for_service("/"+self._uuv_name+"/get_model_properties")
        self._get_model_props = rospy.ServiceProxy("/"+self._uuv_name+"/get_model_properties", GetModelProperties)
        self._linkloads = dict()
        self._linkinertias = dict()
        hydromodel = self._get_model_props()
        rho = hydromodel.models[0].fluid_density
        g = 9.806
        for index, name in enumerate(hydromodel.link_names):
            if not 'base' in name:
                B = rho * g * hydromodel.models[index].volume
                I = hydromodel.models[index].inertia
                M = np.zeros((6,6))
                np.fill_diagonal(M, (I.m, I.m, I.m, I.ixx, I.iyy, I.izz))
                self._linkloads[name] = np.matrix([0, 0, -I.m + B, 0, 0, 0]).T
                self._linkinertias[name] = np.asmatrix(M)

if __name__ == '__main__':
    # Start the node
    node_name = os.path.splitext(os.path.basename(__file__))[0]
    rospy.init_node(node_name)
    rospy.loginfo('Starting [%s] node' % node_name)

    smc_controller = SMC()

    rospy.spin()
    rospy.loginfo('Shutting down [%s] node' % node_name)
