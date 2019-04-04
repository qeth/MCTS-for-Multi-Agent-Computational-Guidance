import math
import numpy as np
import random
import gym
from gym import spaces
from gym.utils import seeding
from collections import OrderedDict

from config_vertiport import Config

__author__ = "Xuxi Yang <xuxiyang@iastate.edu>"


class MultiAircraftEnv(gym.Env):
    """
    This is the airspace simulator where we can control multiple aircraft to their respective
    goal position while avoiding conflicts between each other.
    **STATE:**
    The state consists all the information needed for the aircraft to choose an optimal action:
    position, velocity, speed, heading, goal position, of each aircraft.
    In the beginning of each episode, all the aircraft and their goals are initialized randomly.
    **ACTIONS:**
    The action is either applying +1, 0 or -1 for the change of heading angle of each aircraft.
    """

    def __init__(self, sd):
        self.load_config()
        self.load_vertiport()
        self.state = None
        self.viewer = None

        # build observation space and action space
        self.observation_space = self.build_observation_space()
        self.position_range = spaces.Box(
            low=np.array([0, 0]),
            high=np.array([self.window_width, self.window_height]),
            dtype=np.float32)
        self.action_space = spaces.Tuple((spaces.Discrete(3),) * self.num_aircraft)

        self.conflicts = 0
        self.conflict_flag = None
        self.distance_mat = None
        self.seed(sd)

    def seed(self, seed=None):
        np.random.seed(seed)
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def load_config(self):
        # input dim
        self.window_width = Config.window_width
        self.window_height = Config.window_height
        self.num_aircraft = Config.num_aircraft
        self.EPISODES = Config.EPISODES
        self.G = Config.G
        self.tick = Config.tick
        self.scale = Config.scale
        self.minimum_separation = Config.minimum_separation
        self.NMAC_dist = Config.NMAC_dist
        self.horizon_dist = Config.horizon_dist
        self.initial_min_dist = Config.initial_min_dist
        self.goal_radius = Config.goal_radius
        self.init_speed = Config.init_speed
        self.min_speed = Config.min_speed
        self.max_speed = Config.max_speed

    def load_vertiport(self):
        self.vertiport_list = []
        for i in range(Config.vertiport_loc.shape[0]):
            self.vertiport_list.append(VertiPort(id=i, position=Config.vertiport_loc[i]))

    def reset(self):
        # aircraft is stored in this list
        self.aircraft_dict = AircraftDict()
        self.id_tracker = 0

        self.conflicts = 0
        self.goals = 0
        self.NMACs = 0

        return self._get_ob()

    def pressure_reset(self):
        self.conflicts = 0
        # aircraft is stored in this list
        self.aircraft_list = []

        for id in range(self.num_aircraft):
            theta = 2 * id * math.pi / self.num_aircraft
            r = self.window_width / 2 - 10
            x = r * np.cos(theta)
            y = r * np.sin(theta)
            position = (self.window_width / 2 + x, self.window_height / 2 + y)
            goal_pos = (self.window_width / 2 - x, self.window_height / 2 - y)

            aircraft = Aircraft(
                id=id,
                position=position,
                speed=self.init_speed,
                heading=theta + math.pi,
                goal_pos=goal_pos
            )

            self.aircraft_list.append(aircraft)

        return self._get_ob()

    def _get_ob(self):
        s = []
        id = []
        for key, aircraft in self.aircraft_dict.ac_dict.items():
            # (x, y, vx, vy, speed, heading, gx, gy)
            s.append(aircraft.position[0])
            s.append(aircraft.position[1])
            s.append(aircraft.velocity[0])
            s.append(aircraft.velocity[1])
            s.append(aircraft.speed)
            s.append(aircraft.heading)
            s.append(aircraft.goal.position[0])
            s.append(aircraft.goal.position[1])

            id.append(key)

        return np.reshape(s, (-1, 8)), id

    def step(self, a, near_end=False):
        # a is a dictionary: {id, action, ...}
        for id, aircraft in self.aircraft_dict.ac_dict.items():
            try:
                aircraft.step(a[id])
            except KeyError:
                aircraft.step()

        for vertiport in self.vertiport_list:
            vertiport.step()
            if vertiport.clock_counter >= vertiport.time_next_aircraft and not near_end:
                goal_vertiport_id = random.choice([e for e in range(len(self.vertiport_list)) if not e == vertiport.id])
                aircraft = Aircraft(
                    id=self.id_tracker,
                    position=vertiport.position,
                    speed=self.init_speed,
                    heading=self.random_heading(),
                    goal_pos=self.vertiport_list[goal_vertiport_id].position
                )
                dist_array, id_array = self.dist_to_all_aircraft(aircraft)
                min_dist = min(dist_array) if dist_array.shape[0] > 0 else 9999
                if min_dist > 3 * self.minimum_separation:  # and self.aircraft_dict.num_aircraft < 10:
                    self.aircraft_dict.add(aircraft)
                    self.id_tracker += 1

                    vertiport.generate_interval()

        reward, terminal, info = self._terminal_reward()

        return self._get_ob(), reward, terminal, info

    def _terminal_reward(self):
        """
        determine the reward and terminal for the current transition, and use info. Main idea:
        1. for each aircraft:
          a. if there is no_conflict, return a large penalty and terminate
          b. elif it is out of map, assign its reward as self.out_of_map_penalty, prepare to remove it
          c. elif if it reaches goal, assign its reward as simulator, prepare to remove it
          d. else assign its reward as simulator
        2. accumulates the reward for all aircraft
        3. remove out-of-map aircraft and goal-aircraft
        4. if all aircraft are removed, return reward and terminate
           else return the corresponding reward and not terminate
        """
        reward = 0
        # info = {'n': [], 'c': [], 'w': [], 'g': []}
        info_dist_list = []
        aircraft_to_remove = []  # add goal-aircraft and out-of-map aircraft to this list

        for id, aircraft in self.aircraft_dict.ac_dict.items():
            # calculate min_dist and dist_goal for checking terminal
            dist_array, id_array = self.dist_to_all_aircraft(aircraft)
            min_dist = min(dist_array) if dist_array.shape[0] > 0 else 9999
            info_dist_list.append(min_dist)
            dist_goal = self.dist_goal(aircraft)

            conflict = False
            # set the conflict flag to false for aircraft
            # elif conflict, set penalty reward and conflict flag but do NOT remove the aircraft from list
            for id, dist in zip(id_array, dist_array):
                if dist >= self.minimum_separation:  # safe
                    aircraft.conflict_id_set.discard(id)  # discarding element not in the set won't raise error

                else:  # conflict!!
                    conflict = True
                    if id not in aircraft.conflict_id_set:
                        self.conflicts += 1
                        aircraft.conflict_id_set.add(id)
                        # info['c'].append('%d and %d' % (aircraft.id, id))
                    aircraft.reward = Config.conflict_penalty

            # if NMAC, set penalty reward and prepare to remove the aircraft from list
            if min_dist < self.NMAC_dist:
                # info['n'].append('%d and %d' % (aircraft.id, close_id))
                aircraft.reward = Config.NMAC_penalty
                aircraft_to_remove.append(aircraft)
                self.NMACs += 1
                # aircraft_to_remove.append(self.aircraft_dict.get_aircraft_by_id(close_id))

            # give out-of-map aircraft a penalty, and prepare to remove it
            elif not self.position_range.contains(np.array(aircraft.position)):
                aircraft.reward = Config.wall_penalty
                # info['w'].append(aircraft.id)
                if aircraft not in aircraft_to_remove:
                    aircraft_to_remove.append(aircraft)

            # set goal-aircraft reward according to simulator, prepare to remove it
            elif dist_goal < self.goal_radius:
                aircraft.reward = Config.goal_reward
                # info['g'].append(aircraft.id)
                self.goals += 1
                if aircraft not in aircraft_to_remove:
                    aircraft_to_remove.append(aircraft)

            # for aircraft without NMAC, conflict, out-of-map, goal, set its reward as simulator
            elif not conflict:
                aircraft.reward = Config.step_penalty

            # accumulates reward
            reward += aircraft.reward

        # remove all the out-of-map aircraft and goal-aircraft
        for aircraft in aircraft_to_remove:
            self.aircraft_dict.remove(aircraft)
        # reward = [e.reward for e in self.aircraft_dict]

        return reward, False, info_dist_list

    def render(self, mode='human'):
        from gym.envs.classic_control import rendering
        from colour import Color
        red = Color('red')
        colors = list(red.range_to(Color('green'), self.num_aircraft))

        if self.viewer is None:
            self.viewer = rendering.Viewer(self.window_width, self.window_height)
            self.viewer.set_bounds(0, self.window_width, 0, self.window_height)

        import os
        __location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))

        for id, aircraft in self.aircraft_dict.ac_dict.items():
            aircraft_img = rendering.Image(os.path.join(__location__, 'images/aircraft.png'), 32, 32)
            jtransform = rendering.Transform(rotation=aircraft.heading - math.pi / 2, translation=aircraft.position)
            aircraft_img.add_attr(jtransform)
            r, g, b = colors[aircraft.id % self.num_aircraft].get_rgb()
            aircraft_img.set_color(r, g, b)
            self.viewer.onetime_geoms.append(aircraft_img)

            goal_img = rendering.Image(os.path.join(__location__, 'images/goal.png'), 32, 32)
            jtransform = rendering.Transform(rotation=0, translation=aircraft.goal.position)
            goal_img.add_attr(jtransform)
            goal_img.set_color(r, g, b)
            self.viewer.onetime_geoms.append(goal_img)

        for veriport in self.vertiport_list:
            vertiport_img = rendering.Image(os.path.join(__location__, 'images/verti.png'), 32, 32)
            jtransform = rendering.Transform(rotation=0, translation=veriport.position)
            vertiport_img.add_attr(jtransform)
            self.viewer.onetime_geoms.append(vertiport_img)

        return self.viewer.render(return_rgb_array=False)

    def close(self):
        if self.viewer:
            self.viewer.close()
            self.viewer = None

    def dist_to_all_aircraft(self, aircraft):
        id_list = []
        dist_list = []
        for id, intruder in self.aircraft_dict.ac_dict.items():
            if id != aircraft.id:
                id_list.append(id)
                dist_list.append(self.metric(aircraft.position, intruder.position))

        return np.array(dist_list), np.array(id_list)

    def dist_goal(self, aircraft):
        return self.metric(aircraft.position, aircraft.goal.position)

    def metric(self, pos1, pos2):
        # the distance between two points
        dx = pos1[0] - pos2[0]
        dy = pos1[1] - pos2[1]
        return math.sqrt(dx ** 2 + dy ** 2)

    # def dist(self, pos1, pos2):
    #     return np.linalg.norm(np.array(pos1) - np.array(pos2))

    def random_pos(self):
        return np.random.uniform(
            low=np.array([0, 0]),
            high=np.array([self.window_width, self.window_height])
        )

    def random_speed(self):
        return np.random.uniform(low=self.min_speed, high=self.max_speed)

    def random_heading(self):
        return np.random.uniform(low=0, high=2 * math.pi)

    def build_observation_space(self):
        s = spaces.Dict({
            'pos_x': spaces.Box(low=0, high=self.window_width, shape=(1,), dtype=np.float32),
            'pos_y': spaces.Box(low=0, high=self.window_height, shape=(1,), dtype=np.float32),
            'vel_x': spaces.Box(low=-self.max_speed, high=self.max_speed, shape=(1,), dtype=np.float32),
            'vel_y': spaces.Box(low=-self.max_speed, high=self.max_speed, shape=(1,), dtype=np.float32),
            'speed': spaces.Box(low=self.min_speed, high=self.max_speed, shape=(1,), dtype=np.float32),
            'heading': spaces.Box(low=0, high=2 * math.pi, shape=(1,), dtype=np.float32),
            'goal_x': spaces.Box(low=0, high=self.window_width, shape=(1,), dtype=np.float32),
            'goal_y': spaces.Box(low=0, high=self.window_height, shape=(1,), dtype=np.float32),
        })

        return spaces.Tuple((s,) * self.num_aircraft)


class AircraftDict:
    def __init__(self):
        self.ac_dict = OrderedDict()

    @property
    def num_aircraft(self):
        return len(self.ac_dict)

    def add(self, aircraft):
        assert aircraft.id not in self.ac_dict.keys(), 'aircraft id %d already in dict' % aircraft.id
        self.ac_dict[aircraft.id] = aircraft

    def remove(self, aircraft):
        try:
            del self.ac_dict[aircraft.id]
        except KeyError:
            pass

    def get_aircraft_by_id(self, aircraft_id):
        return self.ac_dict[aircraft_id]


class AircraftList:
    def __init__(self):
        self.ac_list = []
        self.id_list = []

    @property
    def num_aircraft(self):
        return len(self.ac_list)

    def add(self, aircraft):
        self.ac_list.append(aircraft)
        self.id_list.append(aircraft.id)
        assert len(self.ac_list) == len(self.id_list)

        unique, count = np.unique(np.array(self.id_list), return_counts=True)
        assert np.all(count < 2), 'ununique id added to list'

    def remove(self, aircraft):
        try:
            self.ac_list.remove(aircraft)
            self.id_list.remove(aircraft.id)
            assert len(self.ac_list) == len(self.id_list)
        except ValueError:
            pass

    def get_aircraft_by_id(self, aircraft_id):
        index = np.where(np.array(self.id_list) == aircraft_id)[0]
        assert index.shape[0] == 1, 'find multi aircraft with id %d' % aircraft_id
        return self.ac_list[int(index)]

        for aircraft in self.buffer_list:
            if aircraft.id == aircraft_id:
                return aircraft


class Goal:
    def __init__(self, position):
        self.position = position


class Aircraft:
    def __init__(self, id, position, speed, heading, goal_pos):
        self.id = id
        self.position = np.array(position, dtype=np.float32)
        self.speed = speed
        self.heading = heading  # rad
        vx = self.speed * math.cos(self.heading)
        vy = self.speed * math.sin(self.heading)
        self.velocity = np.array([vx, vy], dtype=np.float32)

        self.reward = 0
        self.goal = Goal(goal_pos)
        dx, dy = self.goal.position - self.position
        self.heading = math.atan2(dy, dx)

        self.load_config()

        self.conflict_id_set = set()

    def load_config(self):
        self.G = Config.G
        self.scale = Config.scale
        self.min_speed = Config.min_speed
        self.max_speed = Config.max_speed
        self.speed_sigma = Config.speed_sigma
        self.position_sigma = Config.position_sigma
        self.d_heading = Config.d_heading

    def step(self, a=1):
        self.speed = max(self.min_speed, min(self.speed, self.max_speed))  # project to range
        self.speed += np.random.normal(0, self.speed_sigma)
        self.heading += (a - 1) * self.d_heading
        vx = self.speed * math.cos(self.heading)
        vy = self.speed * math.sin(self.heading)
        self.velocity = np.array([vx, vy])

        self.position += self.velocity


class VertiPort:
    def __init__(self, id, position):
        self.id = id
        self.position = np.array(position)
        self.clock_counter = 0
        self.time_next_aircraft = np.random.uniform(0, 60)

    # when the next aircraft will take off
    def generate_interval(self):
        self.time_next_aircraft = np.random.uniform(Config.time_interval_lower, Config.time_interval_upper)
        self.clock_counter = 0

    # add the clock counter by 1
    def step(self):
        self.clock_counter += 1
