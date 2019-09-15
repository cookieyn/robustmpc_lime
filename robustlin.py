import numpy as np
import fixed_env as env
import load_trace
from sklearn.tree import DecisionTreeClassifier
import limes
import pickle as pk
import get_reward
#import loadsave as io

S_INFO = 5  # bit_rate, buffer_size, rebuffering_time, bandwidth_measurement, chunk_til_video_end
S_LEN = 8  # take how many frames in the past
A_DIM = 6
MPC_FUTURE_CHUNK_COUNT = 5
ACTOR_LR_RATE = 0.0001
CRITIC_LR_RATE = 0.001
VIDEO_BIT_RATE = [300, 750, 1200, 1850, 2850, 4300]  # Kbps
BITRATE_REWARD = [1, 2, 3, 12, 15, 20]
BUFFER_NORM_FACTOR = 10.0
CHUNK_TIL_VIDEO_END_CAP = 48.0
TOTAL_VIDEO_CHUNKS = 48
M_IN_K = 1000.0
REBUF_PENALTY = 4.3  # 1 sec rebuffering -> 3 Mbps
SMOOTH_PENALTY = 1
DEFAULT_QUALITY = 0  # default video quality without agent
RANDOM_SEED = 42
RAND_RANGE = 1000000
SUMMARY_DIR = './results'
LOG_FILE = './results/log_robustlin'
# log in format of time_stamp bit_rate buffer_size rebuffer_time chunk_size download_time reward
# NN_MODEL = './models/nn_model_ep_5900.ckpt'

CHUNK_COMBO_OPTIONS = []

# past errors in bandwidth
past_errors = []
past_bandwidth_ests = []

size_video1 = np.loadtxt('video/video_size_5', dtype=int).tolist()
size_video2 = np.loadtxt('video/video_size_4', dtype=int).tolist()
size_video3 = np.loadtxt('video/video_size_3', dtype=int).tolist()
size_video4 = np.loadtxt('video/video_size_2', dtype=int).tolist()
size_video5 = np.loadtxt('video/video_size_1', dtype=int).tolist()
size_video6 = np.loadtxt('video/video_size_0', dtype=int).tolist()


class Robustlin:
    def __init__(self):
        pass

    def get_chunk_size(self, quality, index):
        if index < 0 or index > 48:
            return 0
        # note that the quality and video labels are inverted (i.e., quality 4 is highest and this pertains to video1)
        sizes = {5: size_video1[index], 4: size_video2[index], 3: size_video3[index], 2: size_video4[index],
                 1: size_video5[index], 0: size_video6[index]}
        return sizes[quality]

    def main(self, args, net_env=None, lime=None):
        np.random.seed(RANDOM_SEED)
        viper_flag = True
        assert len(VIDEO_BIT_RATE) == A_DIM

        if net_env is None:
            viper_flag = False
            all_cooked_time, all_cooked_bw, all_file_names = load_trace.load_trace()
            net_env = env.Environment(all_cooked_time=all_cooked_time, all_cooked_bw=all_cooked_bw,
                                      all_file_names=all_file_names)

        if not viper_flag and args.log:
            log_path = LOG_FILE + '_' + net_env.all_file_names[net_env.trace_idx]
            log_file = open(log_path, 'wb')

        time_stamp = 0

        last_bit_rate = DEFAULT_QUALITY
        bit_rate = DEFAULT_QUALITY

        action_vec = np.zeros(A_DIM)
        action_vec[bit_rate] = 1

        s_batch = [np.zeros((S_INFO, S_LEN))]
        a_batch = [action_vec]
        r_batch = []
        rollout = []
        entropy_record = []

        video_count = 0

        # load dt policy
        if lime is None:
            with open('lime/robustmpc.pk3', 'rb') as f:                   
               lime = pk.load(f)

        while True:  # serve video forever
            # the action is from the last decision
            # this is to make the framework similar to the real

            delay, sleep_time, buffer_size, rebuf, video_chunk_size, next_video_chunk_sizes, end_of_video, \
            video_chunk_remain = net_env.get_video_chunk(bit_rate)

            time_stamp += delay  # in ms
            time_stamp += sleep_time  # in ms

            reward = get_reward.get_reward(bit_rate, rebuf, last_bit_rate, args.qoe_metric)
            r_batch.append(reward)
            last_bit_rate = bit_rate

            if args.log:
                # log time_stamp, bit_rate, buffer_size, reward
                log_file.write(bytes(str(time_stamp / M_IN_K) + '\t' +
                                     str(VIDEO_BIT_RATE[bit_rate]) + '\t' +
                                     str(buffer_size) + '\t' +
                                     str(rebuf) + '\t' +
                                     str(video_chunk_size) + '\t' +
                                     str(delay) + '\t' +
                                     str(reward) + '\n', encoding='utf-8'))
                log_file.flush()

            # retrieve previous state
            if len(s_batch) == 0:
                state = [np.zeros((S_INFO, S_LEN))]
            else:
                state = np.array(s_batch[-1], copy=True)

            # dequeue history record
            state = np.roll(state, -1, axis=1)

            # this should be S_INFO number of terms
            state[0, -1] = VIDEO_BIT_RATE[bit_rate] / float(np.max(VIDEO_BIT_RATE))  # last quality
            state[1, -1] = buffer_size / BUFFER_NORM_FACTOR
            state[2, -1] = rebuf
            state[3, -1] = float(video_chunk_size) / float(delay) / M_IN_K  # kilo byte / ms
            state[4, -1] = np.minimum(video_chunk_remain, CHUNK_TIL_VIDEO_END_CAP) / float(CHUNK_TIL_VIDEO_END_CAP)
            # state[5: 10, :] = future_chunk_sizes / M_IN_K / M_IN_K

            serialized_state = []
            # Log input of neural network
            serialized_state.append(state[0, -1])
            serialized_state.append(state[1, -1])
            serialized_state.append(state[2, -1])
            for i in range(5):
                serialized_state.append(state[3, i])
            serialized_state.append(state[4, -1])

            bit_rate = int(lime.predict(np.array(serialized_state).reshape(1, -1))[0])
            rollout.append((state, bit_rate, serialized_state))
            s_batch.append(state)

            if end_of_video:
                if args.log:
                    log_file.write(bytes('\n', 'utf-8'))
                    log_file.close()

                last_bit_rate = DEFAULT_QUALITY
                bit_rate = DEFAULT_QUALITY  # use the default action here

                del s_batch[:]
                del a_batch[:]
                del r_batch[:]

                action_vec = np.zeros(A_DIM)
                action_vec[bit_rate] = 1

                s_batch.append(np.zeros((S_INFO, S_LEN)))
                a_batch.append(action_vec)
                entropy_record = []

                if viper_flag:
                    break
                else:
                    video_count += 1
                    print("video count", video_count)
                    if video_count >= len(net_env.all_file_names):
                        break
                    if args.log:
                        log_path = LOG_FILE + '_' + net_env.all_file_names[net_env.trace_idx]
                        log_file = open(log_path, 'wb')


        return rollout
