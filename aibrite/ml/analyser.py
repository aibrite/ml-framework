import concurrent.futures
import time
from aibrite.ml.core import TrainResult, MlBase, PredictionResult, TrainIteration
from aibrite.ml.neuralnet import NeuralNet
import uuid
import pandas as pd
import os
import datetime
from threading import Lock
from collections import namedtuple

analyser_cache = {}

JobResult = namedtuple('JobResult', 'train_data prediction_data')


class AnalyserJob:

    def __init__(self):
        self.id = str(uuid.uuid4())
        self.status = 'created'
        self._predlock = Lock()
        self._trainlock = Lock()
        self._prediction_data = []
        self._train_data = []

    def add_to_train_log(self, neuralnet, train_data, extra_data=None):
        extra_data = extra_data if extra_data != None else {}
        hyper_parameters = neuralnet.get_hyperparameters()
        now = datetime.datetime.now()

        base_cols = {
            'timestamp': now,
            'classifier': neuralnet.__class__.__name__,
            'classifier_id': neuralnet.instance_id
        }

        data = {**base_cols, **train_data, **hyper_parameters, **extra_data}
        with self._trainlock:
            self._train_data.append(data)
        return data

    def add_to_prediction_log(self, neuralnet, test_set_id, score, extra_data=None):
        extra_data = extra_data if extra_data != None else {}
        precision, recall, f1, support = score.totals
        hyper_parameters = neuralnet.get_hyperparameters()
        now = datetime.datetime.now()

        for i, v in enumerate(score.labels):
            base_cols = {
                'timestamp': now,
                'classifier': neuralnet.__class__.__name__,
                'classifier_id': neuralnet.instance_id,
                'test_set': test_set_id,
                'precision': score.precision[i],
                'recall': score.recall[i],
                'accuracy': score.accuracy,
                'f1': score.f1[i],
                'label': score.labels[i],
                'support': score.support[i],
                'job_id': 1
            }

            data = {**base_cols, **hyper_parameters, **extra_data}

        base_cols = {
            'timestamp': now,
            'classifier': neuralnet.__class__.__name__,
            'classifier_id': neuralnet.instance_id,
            'test_set': test_set_id,
            'precision': precision,
            'recall': recall,
            'accuracy': score.accuracy,
            'f1': f1,
            'support': support,
            'label': '__totals__',
            'job_id': 1

        }

        data = {**base_cols, **hyper_parameters, **extra_data}
        with self._predlock:
            self._prediction_data.append(data)
        return data


class NeuralNetAnalyser:

    def save_logs(self):
        pred_file = os.path.join(self.log_dir, 'pred.csv')
        train_file = os.path.join(self.log_dir, 'train.csv')

        self.prediction_log.to_csv(pred_file)
        self.train_log.to_csv(train_file)

    def _init_logs(self):
        self.prediction_log = pd.DataFrame(columns=[
            'timestamp', 'classifier', 'classifier_id', 'test_set', 'label', 'f1', 'precision', 'recall', 'accuracy', 'support'])

        self.train_log = pd.DataFrame(columns=[
            'timestamp', 'classifier', 'classifier_id', 'cost', 'epoch', 'current_minibatch_index'])

        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)

    def _append_job_data(self, train_data, prediction_data):
        for item in prediction_data:
            self.prediction_log = self.prediction_log.append(
                item, ignore_index=True)
        for item in train_data:
            self.train_log = self.train_log.append(
                item, ignore_index=True)

    def __init__(self, log_dir='./', max_workers=None, executor=concurrent.futures.ProcessPoolExecutor, train_options=None, job_completed=None):
        self.executor = executor(max_workers=None)
        self.worker_list = []
        self.job_list = {}

        self.log_dir = log_dir if log_dir != None else './'
        self.log_dir = os.path.join(
            self.log_dir, datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S-%f'))
        self._init_logs()
        self.train_options = train_options if train_options != None else {
            'foo': 12
        }
        self.job_completed = job_completed
        self.id = str(uuid.uuid4())
        analyser_cache[self.id] = self

    def _start_job(analyser_id, neuralnet_class, train_set, test_sets, **kvargs):
        analyser = analyser_cache[analyser_id]
        job = AnalyserJob()
        analyser.job_list[job.id] = job

        train_x, train_y = train_set
        neuralnet = neuralnet_class(train_x, train_y, **kvargs)

        job.status = 'training:started'
        neuralnet.train(lambda neuralnet, train_data: job.add_to_train_log(
            neuralnet, train_data._asdict()))
        job.status = 'prediction:started'
        for test_set_id, test_set in test_sets.items():
            test_set_x, test_set_y = test_set
            prediction_result = neuralnet.predict(test_set_x)
            score = NeuralNet.score_report(
                test_set_y, prediction_result.predicted, labels=neuralnet.labels)
            job.add_to_prediction_log(neuralnet, test_set_id, score)
            # print("{0}:\n{1}\n".format(
            #     job.neuralnet, NeuralNet.format_score_report(score)))

        job.status = 'completed'
        return job._train_data, job._prediction_data

    def submit(self, neuralnet_class, train_set, test_sets, **kvargs):
        item = self.executor.submit(
            NeuralNetAnalyser._start_job, self.id, neuralnet_class, train_set, test_sets, **kvargs)
        self.worker_list.append(item)

    def start(self):
        for future in self._as_completed():
            try:
                train_data, prediction_data = future.result()
            except Exception as exc:
                print("ERROR")
                print(exc)
                # raise exc
                self.worker_list.remove(future)
            else:
                self._append_job_data(train_data, prediction_data)
                if self.job_completed != None:
                    self.job_completed(self, JobResult(
                        train_data=train_data, prediction_data=prediction_data))
                self.save_logs()

    def _as_completed(self):
        return concurrent.futures.as_completed(self.worker_list)

    def print_summary(self):
        pred_totals = self.prediction_log[self.prediction_log['label']
                                          == '__totals__'].sort_values(['f1'], ascending=False)

        print("*" * 32)
        print("{:^32}".format("PREDICTION SUMMARY"))
        print("*" * 32)

        print("Predictions:")

        with pd.option_context('expand_frame_repr', False):
            print(
                pred_totals[['classifier', 'test_set', 'f1', 'iteration_count']])
