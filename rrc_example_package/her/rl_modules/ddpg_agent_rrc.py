import torch
import os
from datetime import datetime
import numpy as np
from mpi4py import MPI
from rrc_example_package.her.mpi_utils.mpi_utils import sync_networks, sync_grads
from rrc_example_package.her.rl_modules.replay_buffer import replay_buffer
from rrc_example_package.her.rl_modules.models import actor, critic
from rrc_example_package.her.mpi_utils.normalizer import normalizer
from rrc_example_package.her.her_modules.her import her_sampler
from rrc_example_package.utils import CsvCreator,init_kinematics,process_inputs
import time
import pybullet as p
from copy import copy
from scipy.spatial.transform import Rotation as R


_CUBE_WIDTH = 0.065
# Set the distance between the center of the cube to the surface of the cube a bit bigger than the real value.
_cube_3d_radius = 1.2 * _CUBE_WIDTH * np.sqrt(3) / 2  

"""
ddpg with HER (MPI-version)

"""
class ddpg_agent_rrc:
    def __init__(self, args, env, env_params):
        self.args = args
        self.env = env
        self.env_params = env_params
        self.kinematics = init_kinematics()
        # create the normalizer
        self.o_norm = normalizer(size=env_params['obs'], default_clip_range=self.args.clip_range)
        self.g_norm = normalizer(size=env_params['goal'], default_clip_range=self.args.clip_range)
        # create the network
        self.actor_network = actor(env_params)
        self.critic_network = critic(env_params)
        
        if self.args.teach_mode == 'teach_collect':
            print('Loading the teacher for collecting the experience')
            self.teach_actor_network = actor(env_params)
            self.t_o_mean, self.t_o_std, self.t_g_mean, self.t_g_std, actor_network_dict,_ = torch.load(self.args.teach_ac_model_path)
            self.teach_actor_network.load_state_dict(actor_network_dict)
            self.teach_actor_network.eval()
            # a_loss: 1.032 q_loss: 0.761 rrc: -2322 rrc_pos: -1696 rrc_ori: -2948 z_mean: -0.789 xy: 1.406 ori: 2.052
        elif self.args.teach_mode == 'actor_critic':
            print('Loading the actor and critic from the teacher')
            o_mean, o_std, g_mean, g_std, actor_network_dict, critic_network_dict = torch.load(self.args.teach_ac_model_path)
            self.o_norm.mean = o_mean
            self.o_norm.std = o_std
            self.g_norm.mean = g_mean
            self.g_norm.std = g_std
            self.actor_network.load_state_dict(actor_network_dict)
            self.critic_network.load_state_dict(critic_network_dict)
            #a_loss: 28.156 q_loss: 7.560 rrc: -2114 rrc_pos: -1264 rrc_ori: -2964 z_mean: -0.740 xy: 0.756 ori: 2.067
        elif self.args.teach_mode == 'actor':
            print('Loading the actor from the teacher')
            o_mean, o_std, g_mean, g_std, actor_network_dict, critic_network_dict = torch.load(self.args.teach_ac_model_path)
            self.o_norm.mean = o_mean
            self.o_norm.std = o_std
            self.g_norm.mean = g_mean
            self.g_norm.std = g_std
            self.actor_network.load_state_dict(actor_network_dict)
            #a_loss: 1.438 q_loss: 0.630 rrc: -2107 rrc_pos: -1334 rrc_ori: -2880 z_mean: -0.776 xy: 0.805 ori: 2.004
            
        # sync the networks across the cpus
        sync_networks(self.actor_network)
        sync_networks(self.critic_network)
        # build up the target network
        self.actor_target_network = actor(env_params)
        self.critic_target_network = critic(env_params)
        # load the weights into the target networks
        self.actor_target_network.load_state_dict(self.actor_network.state_dict())
        self.critic_target_network.load_state_dict(self.critic_network.state_dict())
        # if use gpu
        if self.args.cuda:
            self.actor_network.cuda()
            self.critic_network.cuda()
            self.actor_target_network.cuda()
            self.critic_target_network.cuda()
        # create the optimizer
        self.actor_optim = torch.optim.Adam(self.actor_network.parameters(), lr=self.args.lr_actor)
        self.critic_optim = torch.optim.Adam(self.critic_network.parameters(), lr=self.args.lr_critic)
        # her sampler
        self.her_module = her_sampler(self.args.replay_strategy, self.args.replay_k, self.env.compute_reward, self.env.steps_per_goal, self.args.trajectory_aware,args = self.args)
        # create the replay buffer
        self.buffer = replay_buffer(self.env_params, self.args.buffer_size, self.her_module.sample_her_transitions)
        # path to save the model
        self.model_path = os.path.join(self.args.save_dir, self.args.exp_dir)
        self.csv = CsvCreator()
        # create the dict for store the model
        if MPI.COMM_WORLD.Get_rank() == 0:
            if not os.path.exists(self.args.save_dir):
                os.mkdir(self.args.save_dir)
            if not os.path.exists(self.model_path):
                os.mkdir(self.model_path)
        
    def learn(self):
        """
        train the network
        """
        if MPI.COMM_WORLD.Get_rank() == 0:
            print('\n[{}] Beginning RRC HER training, difficulty = {}\n'.format(datetime.now(), self.args.difficulty))
        # start to collect samples
        for epoch in range(self.args.n_epochs):
                self.epoch = epoch
                actor_loss, critic_loss, explore_success,explore_success_pos,explore_success_ori = [],[],[],[],[]
                for _ in range(self.args.n_cycles):
                    mb_obs, mb_ag, mb_g, mb_actions = [], [], [], []
                    for _ in range(self.args.num_rollouts_per_mpi):
                        # reset the rollouts
                        ep_obs, ep_ag, ep_g, ep_actions = [], [], [], []
                        # reset the environment
                        observation = self.env.reset(difficulty=self.args.difficulty, noisy=self.args.noisy_resets, noise_level=self.args.noise_level)
                        # p.resetDebugVisualizerCamera(cameraDistance=0.45, cameraYaw=135, cameraPitch=-45.0, cameraTargetPosition=[0, 0.0, 0.0])
                        obs = observation['observation']
                        ag = observation['achieved_goal']
                        g = observation['desired_goal']
                        # start to collect samples
#################################Teach collect ################################
                        if self.args.teach_mode == 'teach_collect':
                            radnum = np.random.uniform(0.0,1.0) # Take the possibility
                            if epoch < 50:
                                prt = 0.9
                            elif epoch >= 50 and epoch <100:
                                prt = 0.9 - ((epoch-50) * (0.80/50))
                            elif epoch >= 100 and epoch <200:
                                prt = 0.1
                            else:
                                prt = 0
                            if radnum < prt:
                                radnum2 = np.random.uniform(0.0,1.0)
                                if radnum2 <= 0.05:
                                    for t in range(self.env_params['max_timesteps']):
                                        with torch.no_grad():
                                            t_g = copy(g)
                                            t_obs = copy(obs)
                                            input_tensor = process_inputs(t_obs, t_g, self.t_o_mean, self.t_o_std, self.t_g_mean, self.t_g_std)
                                            pi = self.teach_actor_network(input_tensor)
                                            action = pi.detach().cpu().numpy().squeeze()
                                        # feed the actions into the environment
                                        observation_new, _, _, info = self.env.step(action)
                                        obs_new = observation_new['observation']
                                        ag_new = observation_new['achieved_goal']
                                        g_new = observation_new['desired_goal']
                                        # append rollouts
                                        ep_obs.append(obs.copy())
                                        ep_ag.append(ag.copy())
                                        ep_g.append(g.copy())
                                        ep_actions.append(action.copy())
                                        # re-assign the observation
                                        obs = obs_new
                                        ag = ag_new
                                        g = g_new
                                else:
                                    for t in range(self.env_params['max_timesteps']):
                                        with torch.no_grad():
                                            t_g = copy(g)
                                            t_obs = copy(obs)
                                            input_tensor = process_inputs(t_obs, t_g, self.t_o_mean, self.t_o_std, self.t_g_mean, self.t_g_std)
                                            pi = self.teach_actor_network(input_tensor)
                                            action = self._select_actions(pi)
                                        # feed the actions into the environment
                                        observation_new, _, _, info = self.env.step(action)
                                        obs_new = observation_new['observation']
                                        ag_new = observation_new['achieved_goal']
                                        g_new = observation_new['desired_goal']
                                        # append rollouts
                                        ep_obs.append(obs.copy())
                                        ep_ag.append(ag.copy())
                                        ep_g.append(g.copy())
                                        ep_actions.append(action.copy())
                                        # re-assign the observation
                                        obs = obs_new
                                        ag = ag_new
                                        g = g_new
                            elif radnum >= prt:
                                for t in range(self.env_params['max_timesteps']):
                                    with torch.no_grad():
                                        input_tensor = self._preproc_inputs(obs, g)
                                        pi = self.actor_network(input_tensor)
                                        action = self._select_actions(pi)
                                    # feed the actions into the environment
                                    observation_new, _, _, info = self.env.step(action)
                                    obs_new = observation_new['observation']
                                    ag_new = observation_new['achieved_goal']
                                    g_new = observation_new['desired_goal']
                                    # append rollouts
                                    ep_obs.append(obs.copy())
                                    ep_ag.append(ag.copy())
                                    ep_g.append(g.copy())
                                    ep_actions.append(action.copy())
                                    # re-assign the observation
                                    obs = obs_new
                                    ag = ag_new
                                    g = g_new
#################################else################################
                        else:
                            for t in range(self.env_params['max_timesteps']):
                                with torch.no_grad():
                                    input_tensor = self._preproc_inputs(obs, g)
                                    pi = self.actor_network(input_tensor)
                                    action = self._select_actions(pi)
                                # feed the actions into the environment
                                observation_new, _, _, info = self.env.step(action)
                                obs_new = observation_new['observation']
                                ag_new = observation_new['achieved_goal']
                                g_new = observation_new['desired_goal']
                                # append rollouts
                                ep_obs.append(obs.copy())
                                ep_ag.append(ag.copy())
                                ep_g.append(g.copy())
                                ep_actions.append(action.copy())
                                # re-assign the observation
                                obs = obs_new
                                ag = ag_new
                                g = g_new
                        ep_obs.append(obs.copy())
                        ep_ag.append(ag.copy())
                        ep_g.append(g.copy())
                        mb_obs.append(ep_obs)
                        mb_ag.append(ep_ag)
                        mb_g.append(ep_g)
                        mb_actions.append(ep_actions)
                        explore_success.append(info['is_success']*1)
                        explore_success_pos.append(info['pos_is_success']*1)
                        explore_success_ori.append(info['ori_is_success']*1)
                    # convert them into arrays
                    mb_obs = np.array(mb_obs)
                    mb_ag = np.array(mb_ag)
                    mb_g = np.array(mb_g)
                    mb_actions = np.array(mb_actions)
                    # store the episodes
                    self.buffer.store_episode([mb_obs, mb_ag, mb_g, mb_actions])
                    self._update_normalizer([mb_obs, mb_ag, mb_g, mb_actions])
                    for _ in range(self.args.n_batches):
                        # train the network
                        a_loss, q_loss = self._update_network()
                        actor_loss += [a_loss]
                        critic_loss += [q_loss]
                    # soft update
                    self._soft_update_target_network(self.actor_target_network, self.actor_network)
                    self._soft_update_target_network(self.critic_target_network, self.critic_network)
                # start to do the evaluation
                explore_success = MPI.COMM_WORLD.allreduce(np.mean(explore_success), op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
                explore_success_pos = MPI.COMM_WORLD.allreduce(np.mean(explore_success_pos), op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
                explore_success_ori = MPI.COMM_WORLD.allreduce(np.mean(explore_success_ori), op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
                success_rate,pos_success_rate,ori_success_rate = self._eval_agent()
                self.save_model(epoch)
                if MPI.COMM_WORLD.Get_rank() == 0:
                    print('[{}] epoch: {} eval_rate: {:.3f} eval_pos_rate: {:.3f} eval_ori_rate: {:.3f} explore_rate: {:.3f} explore_pos_rate: {:.3f} explore_ori_rate: {:.3f} a_loss: {:.3f} q_loss: {:.3f} rrc: {:.0f} rrc_pos: {:.0f} rrc_ori: {:.0f} z_mean: {:.3f} xy: {:.3f} ori: {:.3f}'\
                          .format(datetime.now(), epoch, success_rate,pos_success_rate,ori_success_rate, explore_success,explore_success_pos,explore_success_ori, np.mean(copy(actor_loss)), np.mean(copy(critic_loss)), self.rrc,self.rrc_pos,self.rrc_ori, self.z, self.xy, self.ori))
                    log = [epoch,success_rate,pos_success_rate,ori_success_rate, explore_success,explore_success_pos,explore_success_ori, np.mean(copy(actor_loss)), np.mean(copy(critic_loss)), self.rrc,self.rrc_pos,self.rrc_ori, self.z, self.xy,self.ori]
                    self.csv.update(log=log,path=self.model_path+'/log.csv')

    def save_model(self, epoch):
        if epoch % self.args.save_interval == 0 and MPI.COMM_WORLD.Get_rank() == 0:
            # Save actor critic
            torch.save([self.o_norm.mean, self.o_norm.std, self.g_norm.mean, self.g_norm.std, self.actor_network.state_dict(), self.critic_network.state_dict()], \
                        self.model_path + '/acmodel{}.pt'.format(epoch))
            # Save optimizers
            torch.save([self.actor_optim.state_dict(), self.critic_optim.state_dict()], self.model_path + '/ac_optimizers.pt')
            # Save target nets
            torch.save([self.actor_target_network.state_dict(), self.critic_target_network.state_dict()], \
                        self.model_path + '/ac_targets.pt')
            # Save the norm
            self.o_norm.save(self.model_path + '/o_norm')
            self.g_norm.save(self.model_path + '/g_norm')
        
    # pre_process the inputs
    def _preproc_inputs(self, obs, g):
        obs_norm = self.o_norm.normalize(obs)
        g_norm = self.g_norm.normalize(g)
        # concatenate the stuffs
        inputs = np.concatenate([obs_norm, g_norm])
        inputs = torch.tensor(inputs, dtype=torch.float32).unsqueeze(0)
        if self.args.cuda:
            inputs = inputs.cuda()
        return inputs
    
    # this function will choose action for the agent and do the exploration
    def _select_actions(self, pi):
        action = pi.cpu().numpy().squeeze()
        # add the gaussian
        action += self.args.noise_eps * self.env_params['action_max'] * np.random.randn(*action.shape)
        action = np.clip(action, -self.env_params['action_max'], self.env_params['action_max'])
        # random actions...
        random_actions = np.random.uniform(low=-self.env_params['action_max'], high=self.env_params['action_max'], \
                                            size=self.env_params['action'])
        # choose if use the random actions
        action += np.random.binomial(1, self.args.random_eps, 1)[0] * (random_actions - action)
        return action

    # update the normalizer
    def _update_normalizer(self, episode_batch):
        mb_obs, mb_ag, mb_g, mb_actions = episode_batch
        mb_obs_next = mb_obs[:, 1:, :]
        mb_ag_next = mb_ag[:, 1:, :]
        mb_g_next = mb_g[:, 1:, :]
        # get the number of normalization transitions
        num_transitions = mb_actions.shape[1] # Only using one rollout????
        # create the new buffer to store them
        buffer_temp = {'obs': mb_obs, 
                       'ag': mb_ag,
                       'g': mb_g[:, :-1, :], 
                       'actions': mb_actions, 
                       'obs_next': mb_obs_next,
                       'ag_next': mb_ag_next,
                        'g_next': mb_g_next
                       }
        transitions = self.her_module.sample_her_transitions(buffer_temp, num_transitions,self.epoch)
        obs, g = transitions['obs'], transitions['g']
        # pre process the obs and g
        transitions['obs'], transitions['g'] = self._preproc_og(obs, g)
        # update
        self.o_norm.update(transitions['obs'])
        self.g_norm.update(transitions['g'])
        # recompute the stats
        self.o_norm.recompute_stats()
        self.g_norm.recompute_stats()

    def _preproc_og(self, o, g):
        o = np.clip(o, -self.args.clip_obs, self.args.clip_obs)
        g = np.clip(g, -self.args.clip_obs, self.args.clip_obs)
        return o, g

    # soft update
    def _soft_update_target_network(self, target, source):
        for target_param, param in zip(target.parameters(), source.parameters()):
            target_param.data.copy_((1 - self.args.polyak) * param.data + self.args.polyak * target_param.data)

    # update the network
    def _update_network(self):
        # sample the episodes
        transitions = self.buffer.sample(self.args.batch_size,self.epoch)
        if self.args.reward_type == "po_z" or self.args.reward_type == "p_o_z": 
            transitions['r'] += self.get_z_reward(transitions['obs'], transitions['g'])
        # pre-process the observation and goal
        o, o_next, g, g_next = transitions['obs'], transitions['obs_next'], transitions['g'], transitions['g_next']
        transitions['obs'], transitions['g'] = self._preproc_og(o, g)
        transitions['obs_next'], transitions['g_next'] = self._preproc_og(o_next, g_next)
        # start to do the update
        obs_norm = self.o_norm.normalize(transitions['obs'])
        g_norm = self.g_norm.normalize(transitions['g'])
        inputs_norm = np.concatenate([obs_norm, g_norm], axis=1)
        obs_next_norm = self.o_norm.normalize(transitions['obs_next'])
        g_next_norm = self.g_norm.normalize(transitions['g_next'])
        inputs_next_norm = np.concatenate([obs_next_norm, g_next_norm], axis=1)
        # transfer them into the tensor
        inputs_norm_tensor = torch.tensor(inputs_norm, dtype=torch.float32)
        inputs_next_norm_tensor = torch.tensor(inputs_next_norm, dtype=torch.float32)
        actions_tensor = torch.tensor(transitions['actions'], dtype=torch.float32)
        r_tensor = torch.tensor(transitions['r'], dtype=torch.float32)
        if self.args.cuda:
            inputs_norm_tensor = inputs_norm_tensor.cuda()
            inputs_next_norm_tensor = inputs_next_norm_tensor.cuda()
            actions_tensor = actions_tensor.cuda()
            r_tensor = r_tensor.cuda()
        # calculate the target Q value function
        with torch.no_grad():
            # do the normalization
            # concatenate the stuffs
            actions_next = self.actor_target_network(inputs_next_norm_tensor)
            q_next_value = self.critic_target_network(inputs_next_norm_tensor, actions_next)
            q_next_value = q_next_value.detach()
            target_q_value = r_tensor + self.args.gamma * q_next_value
            target_q_value = target_q_value.detach()
            # clip the q value
            clip_return = 1 / (1 - self.args.gamma)
            if self.args.reward_type == "po_z":
                clip_return += 50 # TODO: calculate proper value!!!
            elif self.args.reward_type == "p_o_z":
                clip_return += 100 # TODO: calculate proper value!!!
            target_q_value = torch.clamp(target_q_value, -clip_return, 0)
        # the q loss
        real_q_value = self.critic_network(inputs_norm_tensor, actions_tensor)
        critic_loss = (target_q_value - real_q_value).pow(2).mean()
        # the actor loss
        actions_real = self.actor_network(inputs_norm_tensor)
        actor_loss = -self.critic_network(inputs_norm_tensor, actions_real).mean()
        actor_loss += self.args.action_l2 * (actions_real / self.env_params['action_max']).pow(2).mean()
        # start to update the network
        self.actor_optim.zero_grad()
        actor_loss.backward()
        sync_grads(self.actor_network)
        self.actor_optim.step()
        # update the critic_network
        self.critic_optim.zero_grad()
        critic_loss.backward()
        sync_grads(self.critic_network)
        self.critic_optim.step()
        
        return actor_loss.detach().numpy(), critic_loss.detach().numpy()
    
    def get_z_reward(self, obs, g):
        obs = np.expand_dims(obs[...,self.env.z_pos], axis=-1)
        g = np.expand_dims(g[...,2], axis=-1)
        z_dist = np.abs(g - obs)
        # punish less if above goal
        scale = g > obs
        scale = (scale + 1) / 2
        # reward is negative of z distance
        r_z = -self.args.z_scale * scale * z_dist
        return r_z
    
    def get_tip_reward(self,obs):
        angular_poses = obs[...,:9]
        tip_poses = []
        for angular_pos in angular_poses:
            tip_poses.append(self.kinematics.forward_kinematics(angular_pos))
        tip_poses = np.array(tip_poses)
        tip0 = tip_poses[:,[0]].squeeze(axis=1)
        tip1 = tip_poses[:,[1]].squeeze(axis=1)
        tip2 = tip_poses[:,[2]].squeeze(axis=1)
        d0 = np.expand_dims(np.linalg.norm(tip0 - obs[...,self.env.x_pos:self.env.x_pos+3], axis=-1), 1)
        rwd = - self.args.tip_ratio * (d0 > _cube_3d_radius).astype(np.float32)
        d1 = np.expand_dims(np.linalg.norm(tip1 - obs[...,self.env.x_pos:self.env.x_pos+3], axis=-1), 1)
        rwd -= self.args.tip_ratio * (d1 > _cube_3d_radius).astype(np.float32)
        d2 = np.expand_dims(np.linalg.norm(tip2 - obs[...,self.env.x_pos:self.env.x_pos+3], axis=-1), 1)
        rwd -= self.args.tip_ratio * (d2 > _cube_3d_radius).astype(np.float32)
        return rwd
    
    # do the evaluation (and store the eval episodes in buffer)
    def _eval_agent(self):
        total_success_rate = []
        total_pos_success_rate = []
        total_ori_success_rate = []
        r_z, xy,r_ori, rrc,rrc_pos,rrc_ori =[], [], [], [],[],[]
        mb_obs, mb_ag, mb_g, mb_actions = [], [], [], []
        for n in range(self.args.n_test_rollouts):
            # reset the rollouts
            ep_obs, ep_ag, ep_g, ep_actions = [], [], [], []
            per_success_rate = []
            pos_success_rate = []
            ori_success_rate = []
            observation = self.env.reset(difficulty=self.args.difficulty, noisy=self.args.noisy_resets, noise_level=self.args.noise_level)
            obs = observation['observation']
            ag = observation['achieved_goal']
            g = observation['desired_goal']
            for _ in range(self.env_params['max_timesteps']):
                with torch.no_grad():
                    input_tensor = self._preproc_inputs(obs, g)
                    pi = self.actor_network(input_tensor)
                    # convert the actions
                    actions = pi.detach().cpu().numpy().squeeze()
                observation_new, r, _, info = self.env.step(actions)
                obs_new = observation_new['observation']
                ag_new = observation_new['achieved_goal']
                g_new = observation_new['desired_goal']
                # append rollouts
                ep_obs.append(obs.copy())
                ep_ag.append(ag.copy())
                ep_g.append(g.copy())
                ep_actions.append(actions.copy())
                # re-assign the observation
                obs = obs_new
                ag = ag_new
                g = g_new
                r_z.append(self.get_z_reward(obs, g))
                pos_success_rate.append(info['pos_is_success'])
                ori_success_rate.append(info['ori_is_success'])
                per_success_rate.append(info['is_success'])
                xy.append(np.linalg.norm(ag[0:2] - g[0:2], axis=-1))
                
                rotation_d = R.from_quat(g[...,3:])
                rotation_a = R.from_quat(ag[...,3:])
                error_rot = rotation_d.inv() * rotation_a
                r_ori.append(error_rot.magnitude())
                
            # Append obs
            ep_obs.append(obs.copy())
            ep_ag.append(ag.copy())
            ep_g.append(g.copy())
            mb_obs.append(ep_obs)
            mb_ag.append(ep_ag)
            mb_g.append(ep_g)
            mb_actions.append(ep_actions)
            # Append success rates
            total_pos_success_rate.append(pos_success_rate)
            total_ori_success_rate.append(ori_success_rate)
            total_success_rate.append(per_success_rate)
            rrc.append(info['rrc_reward'])
            rrc_pos.append(info['rrc_reward_pos'])
            rrc_ori.append(info['rrc_reward_ori'])
        # convert them into arrays
        mb_obs = np.array(mb_obs)
        mb_ag = np.array(mb_ag)
        mb_g = np.array(mb_g)
        mb_actions = np.array(mb_actions)
        # store the episodes
        self.buffer.store_episode([mb_obs, mb_ag, mb_g, mb_actions])
        self._update_normalizer([mb_obs, mb_ag, mb_g, mb_actions])
        # Calculate success rates
        
        total_pos_success_rate = np.array(total_pos_success_rate)
        local_pos_success_rate = np.mean(total_pos_success_rate[:, -1])
        global_pos_success_rate = MPI.COMM_WORLD.allreduce(local_pos_success_rate, op=MPI.SUM)
        
        total_ori_success_rate = np.array(total_ori_success_rate)
        local_ori_success_rate = np.mean(total_ori_success_rate[:, -1])
        global_ori_success_rate = MPI.COMM_WORLD.allreduce(local_ori_success_rate, op=MPI.SUM)
        
        total_success_rate = np.array(total_success_rate)
        local_success_rate = np.mean(total_success_rate[:, -1])
        global_success_rate = MPI.COMM_WORLD.allreduce(local_success_rate, op=MPI.SUM)
        self.rrc = MPI.COMM_WORLD.allreduce(np.mean(rrc), op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
        self.rrc_pos = MPI.COMM_WORLD.allreduce(np.mean(rrc_pos), op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
        self.rrc_ori = MPI.COMM_WORLD.allreduce(np.mean(rrc_ori), op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
        self.z = MPI.COMM_WORLD.allreduce(np.mean(r_z), op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
        self.xy = MPI.COMM_WORLD.allreduce(10*np.mean(xy), op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
        self.ori = MPI.COMM_WORLD.allreduce(np.mean(r_ori), op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
        return global_success_rate / MPI.COMM_WORLD.Get_size(), global_pos_success_rate / MPI.COMM_WORLD.Get_size(),global_ori_success_rate / MPI.COMM_WORLD.Get_size()

    def load_model(self,ac_model_path):
        print('loading the model')
        o_mean, o_std, g_mean, g_std, actor_network_dict, critic_network_dict = torch.load(ac_model_path)
        self.o_norm.mean = o_mean
        self.o_norm.std = o_std
        self.g_norm.mean = g_mean
        self.g_norm.std = g_std
        self.actor_network.load_state_dict(actor_network_dict)
        self.critic_network.load_state_dict(critic_network_dict)
