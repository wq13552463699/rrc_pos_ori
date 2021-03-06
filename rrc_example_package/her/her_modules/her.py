import numpy as np

class her_sampler:
    def __init__(self, replay_strategy, replay_k, reward_func=None, steps_per_goal=None, trajectory_aware=False,args=None):
        self.replay_strategy = replay_strategy
        self.replay_k = replay_k
        if self.replay_strategy == 'future':
            self.future_p = 1 - (1. / (1 + replay_k))
        else:
            self.future_p = 0
        self.reward_func = reward_func
        self.steps_per_goal = steps_per_goal
        self.trajectory_aware = trajectory_aware
        self.args = args
    
    def sample_her_transitions(self, episode_batch, batch_size_in_transitions,epoch):
        T = episode_batch['actions'].shape[1]
        rollout_batch_size = episode_batch['actions'].shape[0]
        batch_size = batch_size_in_transitions
        # select which rollouts and which timesteps to be used
        episode_idxs = np.random.randint(0, rollout_batch_size, batch_size)
        t_samples = np.random.randint(T, size=batch_size)
        transitions = {key: episode_batch[key][episode_idxs, t_samples].copy() for key in episode_batch.keys()}
        
        # Only sample 'achieved goals' for HER from time-span of current goal
        T = self.steps_per_goal * np.ceil((t_samples + 1) / self.steps_per_goal)
        
        # her idx
        her_indexes = np.where(np.random.uniform(size=batch_size) < self.future_p)
        future_offset = np.random.uniform(size=batch_size) * (T - t_samples)
        future_offset = future_offset.astype(int)
        future_t = (t_samples + 1 + future_offset)[her_indexes]
        # replace goal with achieved goal
        future_ag = episode_batch['ag'][episode_idxs[her_indexes], future_t]
        temp_g = transitions['g'].copy()

        transitions['g'][her_indexes] = future_ag
        
        if self.args.reward_type == "po_z" or self.args.reward_type == "p_o_z":
            transitions['g'][:,2] = temp_g[:,2]

        # to get the params to re-compute reward
        transitions['r'] = np.expand_dims(self.reward_func(transitions['ag_next'], transitions['g'], self.args.reward_type,epoch), 1)
        transitions = {k: transitions[k].reshape(batch_size, *transitions[k].shape[1:]) for k in transitions.keys()}
        temp_g_next = transitions['g_next'].copy()
        transitions['g_next'] = transitions['g'].copy()
        if self.trajectory_aware:
            # maintain goal changes in transitions where such a change takes place (not advised)
            no_change = np.where(t_samples == (T - 1))
            transitions['g_next'][no_change] = temp_g_next[no_change]

        return transitions
