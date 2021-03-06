from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import copy
import logging

import numpy as np
from builtins import range

from rasa_dm.controller import RegexInterpreter
from rasa_dm.domain import check_domain_sanity
from rasa_dm.events import RevertLastUserUtterance, UserUtterance
from rasa_dm.channels.console import ConsoleInputChannel
from rasa_dm.policies import Policy, PolicyTrainer
from rasa_dm.policies.ensemble import PolicyEnsemble
from rasa_dm.tracker_store import InMemoryTrackerStore
from rasa_dm.util import request_input, str_range_list, bcolors, wrap_with_color, one_hot

logger = logging.getLogger(__name__)


class OnlinePolicyTrainer(PolicyTrainer):
    def train(self, filename=None, interpreter=None, input_channel=None, max_history=3,
              augmentation_factor=20, max_training_samples=None, **kwargs):

        logger.debug("Policy trainer got kwargs: {}".format(kwargs))
        check_domain_sanity(self.domain)

        X, y = self._prepare_training_data(filename, max_history,
                                           augmentation_factor, max_training_samples)

        self.ensemble.train(X, y, self.domain, self.featurizer, **kwargs)

        self.run_online_training(OnlinePolicyEnsemble(self.ensemble, self.featurizer, max_history, (X, y)),
                                 self.domain, interpreter, input_channel)

    def run_online_training(self, ensemble, domain, interpreter=None, input_channel=None):
        from rasa_dm.agent import Agent
        bot = Agent(domain, ensemble,
                    featurizer=self.featurizer,
                    interpreter=interpreter if interpreter else RegexInterpreter())
        bot.toggle_memoization(False)

        bot.handle_channel(input_channel if input_channel else ConsoleInputChannel())


class OnlinePolicyEnsemble(PolicyEnsemble):
    def __init__(self, base_ensemble, featurizer, max_history, train_data, use_visualization=False):
        super(OnlinePolicyEnsemble, self).__init__(base_ensemble.policies)
        self.base_ensemble = base_ensemble
        self.current_id = 0
        self.extra_intent_examples = []
        self.stories = []
        self.featurizer = featurizer

        self.max_history = max_history
        self.batch_size = 5
        self.epochs = 50
        self.train_data = train_data
        self.use_visualization = use_visualization

    def probabilities_using_best_policy(self, tracker, domain):
        # [feature vector, tracker, domain] -> int
        # given a state, predict next action via asking a human
        probabilities = self.base_ensemble.probabilities_using_best_policy(tracker, domain)
        pred_out = np.argmax(probabilities)
        latest_action_was_listen = self._print_history(tracker)

        if latest_action_was_listen:
            print("The bot wants to [{}] due to the intent. Is this correct?\n".format(
                    wrap_with_color(domain.action_for_index(pred_out).name(), bcolors.OKBLUE)))
            user_input = request_input(["1", "2", "3", "0"],
                                       "\t1.\tYes\n" +
                                       "\t2.\tNo, intent is right but the action is wrong\n" +
                                       "\t3.\tThe intent is wrong\n" +
                                       "\t0.\tExport current conversations as stories and quit\n")
        else:
            print("The bot wants to [{}]. Is this correct?\n".format(
                    wrap_with_color(domain.action_for_index(pred_out).name(), bcolors.OKBLUE)))
            user_input = request_input(["1", "2", "0"],
                                       "\t1.\tYes.\n" +
                                       "\t2.\tNo, the action is wrong.\n" +
                                       "\t0.\tExport current conversations as stories and quit\n")

        X = np.expand_dims(np.array(domain.feature_vector_for_tracker(self.featurizer, tracker, self.max_history)), 0)
        if user_input == "1":
            # max prob prediction was correct
            return probabilities
        elif user_input == "2":
            # max prob prediction was false, new action required
            # action wrong
            y = self._request_action(probabilities, domain, tracker)
            self._fit_example(X, y, domain)
            self.write_out_story(tracker)
            return one_hot(y, domain.num_actions)
        elif user_input == "3":
            # intent wrong and maybe action wrong
            intent = self._request_intent(tracker, domain)
            latest_message = copy.copy(tracker.latest_message)
            latest_message.intent = intent
            tracker.log_event(RevertLastUserUtterance())
            tracker.log_event(latest_message)
            return self.probabilities_using_best_policy(tracker, domain)
        elif user_input == "0":
            # export current stories and quit
            tracker.log_event(UserUtterance("_export"))
            exit()
        else:
            raise Exception("Incorrect user input received '{}'".format(user_input))

    def _fit_example(self, X, y, domain):
        # takes the new example labelled and learns it
        # via taking `epochs` samples of n_batch-1 parts of the training data, inserting our new example
        # and learning them. this means that we can ask the network to fit the example without overemphasising
        # its importance (and therefore throwing off the biases)
        train_X, train_y = self.train_data
        for i in range(self.epochs):
            padding_idx = np.random.choice(range(len(train_y)),
                                           replace=False,
                                           size=min(self.batch_size - 1, len(train_y) - 1))
            batch_X = np.vstack((train_X[padding_idx, :, :], X))
            batch_y = np.hstack((train_y[padding_idx], y))
            for p in self.policies:
                p.continue_training(batch_X, np.array(batch_y), domain)
        self.train_data = (np.vstack((train_X, X)), np.hstack((train_y, y)))

    def write_out_story(self, tracker):
        # takes our new example and writes it in markup story format
        self.stories.append(tracker.export_stories())

    def _request_intent(self, tracker, domain):
        # take in some argument and ask which intent it should have been
        # save the intent to a json like file
        print("------\n")
        print("Message:\n")
        print(tracker.latest_message.text)
        print("User said:\t {}".format(wrap_with_color(tracker.latest_message.text, bcolors.OKGREEN)))
        print("What intent is this?\t")
        for idx, intent in enumerate(domain.intents):
            print('\t{}\t{}'.format(idx, intent))
        out = int(request_input(str_range_list(0, len(domain.intents))))
        json_example = {
            'text': tracker.latest_message.text,
            'intent': domain.intents[out]
        }
        self.extra_intent_examples.append(json_example)
        intent_name = domain.intents[out]
        return {'name': intent_name, 'confidence': 1.0}

    def _print_history(self, tracker):
        # prints the historical interactions between the bot and the user, to help with correctly identifying the action
        latest_listen_flag = False
        tr_json = []
        for tr in tracker.generate_all_prior_states():
            tr_json.append({
                'action': tr.latest_action_id_str,
                'intent': tr.latest_message.intent['name'] if tr.latest_message.intent else "",
                'entities': tr.latest_message.entities
            })

        print("------")
        print("Chat history:\n")
        tr_json = tr_json[-self.max_history:]
        n_history = len(tr_json)
        for idx, hist_tracker in enumerate(tr_json):

            print("\tbot did:\t{}\n".format(hist_tracker['action']))
            if hist_tracker['action'] == 'action_listen':
                if idx < n_history - 1:
                    print("\tuser did:\t{}\n".format(hist_tracker['intent']))
                    for entity in hist_tracker['entities']:
                        print("\twith {}:\t{}\n".format(entity['entity'], entity['value']))
                if idx == n_history - 1:
                    print("\tuser said:\t{}\n".format(wrap_with_color(tracker.latest_message.text, bcolors.OKGREEN)))
                    print("\t\t whose intent is:\t{}\n".format(hist_tracker['intent']))
                    for entity in hist_tracker['entities']:
                        print("\twith {}:\t{}\n".format(entity['entity'], entity['value']))
                    latest_listen_flag = True
        slot_strs = ["{}: {}".format(k,
                                     wrap_with_color(str(s.value), bcolors.WARNING)) for k, s in tracker.slots.items()]
        print("we currently have slots: {}\n".format(", ".join(slot_strs)))

        print("------")
        return latest_listen_flag

    def _request_action(self, predictions, domain, tracker):
        # given the intent and the text (NOT IMPLEMENTED) what is the correct action?
        self._print_history(tracker)
        print("what is the next action for the bot?\n")

        for idx in range(domain.num_actions):
            print("{:>10}{:>40}    {:03.2f}".format(idx, domain.action_for_index(idx).name(), predictions[idx]))

        out = int(request_input(str_range_list(0, domain.num_actions)))
        print("thanks! The bot will now [{}]\n -----------".format(domain.action_for_index(out).name()))
        return out

    def test(self, X, y, max_training_samples, num_actions, **kwargs):
        n_test_epochs = 7
        n_examples = len(y)
        i = max_training_samples
        valid_idxs = np.random.choice(range(n_examples), size=200)
        j = 0
        scores = []
        js = []

        while True:
            if i not in valid_idxs:
                self._fit_example([X[i, :]], y[i], num_actions)
            i += 1
            j += 1
            if i == n_examples:
                i = max_training_samples
                n_test_epochs -= 1
                if n_test_epochs == 0:
                    break
            if j % 10 == 0 and self.use_visualization:
                import matplotlib.pyplot as plt
                js.append(j + max_training_samples)
                scores.append(self.score(X[valid_idxs, :], y[valid_idxs]))
                plt.clf()
                plt.plot(js, scores)
                plt.ylabel("percentage of correct next-actions in held-out test set")
                plt.xlabel("number of examples in training set")
                plt.ylim((0, 1))
                for epoc in range(int(np.floor(j / (n_examples - len(valid_idxs))))):
                    plt.axvline((epoc + 1) * (n_examples - len(valid_idxs)), linestyle='--')
                plt.savefig("training_score_with_slots{}.jpg".format(kwargs.get("epochs", 10)))

    def score(self, X, y):
        all_outs = []
        for idx in range(np.shape(X)[0]):
            x = X[[idx], :, :]
            pred_out = self.base_ensemble.predict_next_action(x)
            all_outs.append(pred_out)
        # all_outs == y is a boolean array, np.mean() treats True=1, False=0
        score = np.mean(all_outs == y)
        return score
