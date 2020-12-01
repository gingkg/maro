# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import sys

from maro.communication import Proxy, SessionMessage
from maro.rl.agent.simple_agent_manager import SimpleAgentManager
from maro.rl.storage.column_based_store import ColumnBasedStore
from maro.simulator import Env

from .abs_actor import AbsActor
from .common import MessageTag, PayloadKey


class SimpleActor(AbsActor):
    """Accept roll-out requests and perform roll-out tasks.

    Args:
        env (Env): An Env instance.
        agent_manager (SimpleAgentManager): An AgentManager instance that manages all agents.
        proxy (Proxy):
    """
    def __init__(self, env, agent_manager: SimpleAgentManager, **proxy_params):
        super().__init__(env, **proxy_params)
        self._agent_manager = agent_manager
        self._registry_table.register_event_handler("learner:rollout:1", self.on_rollout_request)

    def on_rollout_request(self, message):
        """Perform local roll-out and send the results back to the request sender.

        Args:
            message: Message containing roll-out parameters and options.
        """
        data = message.payload
        if data.get(PayloadKey.DONE, False):
            sys.exit(0)

        performance, details = self.roll_out(
            model_dict=data[PayloadKey.MODEL],
            exploration_params=data[PayloadKey.EXPLORATION_PARAMS],
            return_details=data[PayloadKey.RETURN_DETAILS]
        )

        self._proxy.reply(
            received_message=message,
            tag=MessageTag.UPDATE,
            payload={PayloadKey.PERFORMANCE: performance, PayloadKey.DETAILS: details}
        )

    def roll_out(
        self, model_dict: dict = None, exploration_params=None, done: bool = False, return_details: bool = True
    ):
        """Perform one episode of roll-out and return performance and experiences.

        Args:
            model_dict (dict): If not None, the agents will load the models from model_dict and use these models
                to perform roll-out.
            exploration_params: Exploration parameters.
            done (bool): If True, the current call is the last call, i.e., no more roll-outs will be performed.
                This flag is used to signal remote actor workers to exit.
            return_details (bool): If True, return experiences as well as performance metrics provided by the env.

        Returns:
            Performance and relevant details from the episode (e.g., experiences).
        """
        if done:
            return None, None

        self._env.reset()

        # load models
        if model_dict is not None:
            self._agent_manager.load_models(model_dict)

        # load exploration parameters:
        if exploration_params is not None:
            self._agent_manager.update_exploration_params(exploration_params)

        metrics, decision_event, is_done = self._env.step(None)
        while not is_done:
            action = self._agent_manager.choose_action(decision_event, self._env.snapshot_list)
            metrics, decision_event, is_done = self._env.step(action)
            self._agent_manager.on_env_feedback(metrics)

        details = self._agent_manager.post_process(self._env.snapshot_list) if return_details else None

        return self._env.metrics, details


class SEEDActor(AbsActor):
    def __init__(self, env, state_shaper, action_shaper, experience_shaper, **proxy_params):
        super().__init__(env, **proxy_params)
        self._env = env
        self._registry_table.register_event_handler("learner:rollout:1", self._on_rollout_request)
        self._state_shaper = state_shaper
        self._action_shaper = action_shaper
        self._experience_shaper = experience_shaper

        # Data structures to temporarily store transitions and trajectory
        self._transition_cache = {}
        self._trajectory = ColumnBasedStore()

    def roll_out(self, return_details: bool = True):
        """Perform local roll-out and send the results back to the request sender.

        Args:
            return_details (bool): If True, return experiences as well as performance metrics provided by the env.
        """
        self._env.reset()
        metrics, decision_event, is_done = self._env.step(None)
        while not is_done:
            action = self._choose_action(decision_event, self._env.snapshot_list)
            metrics, decision_event, is_done = self._env.step(action)
            self._transition_cache["metrics"] = metrics
            self._trajectory.put(self._transition_cache)

        details = self._post_process() if return_details else None
        return self._env.metrics, details

    def _on_rollout_request(self, message):
        data = message.payload
        if data.get(PayloadKey.DONE, False):
            sys.exit(0)

        performance, details = self.roll_out(return_details=message.payload[PayloadKey.RETURN_DETAILS])
        self._proxy.reply(
            received_message=message,
            tag=MessageTag.UPDATE,
            payload={PayloadKey.PERFORMANCE: performance, PayloadKey.DETAILS: details}
        )

    def _choose_action(self, decision_event, snapshot_list):
        agent_id, model_state = self._state_shaper(decision_event, snapshot_list)
        reply = self._proxy.send(
            SessionMessage(
                tag=MessageTag.CHOOSE_ACTION,
                source=self._proxy.Component_name,
                destination=self._proxy.peers_name["learner"][0],
                payload={PayloadKey.STATE: model_state, PayloadKey.AGENT_ID: agent_id},
            )
        )
        model_action = reply[0].payload[PayloadKey.ACTION]
        self._transition_cache = {
            "state": model_state,
            "action": model_action,
            "reward": None,
            "agent_id": agent_id,
            "event": decision_event
        }

        return self._action_shaper(model_action, decision_event, snapshot_list)

    def _post_process(self):
        """Process the latest trajectory into experiences."""
        experiences = self._experience_shaper(self._trajectory, self._env.snapshot_list)
        self._trajectory.clear()
        self._transition_cache = {}
        self._state_shaper.reset()
        self._action_shaper.reset()
        self._experience_shaper.reset()
        return experiences