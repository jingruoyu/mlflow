# pep8: disable=E501

import collections
import pytest
import sys
import pickle
from packaging.version import Version

import numpy as np
import pandas as pd
import tensorflow as tf
from tensorflow.keras import layers

import mlflow
import mlflow.tensorflow
from mlflow.tensorflow._autolog import _TensorBoard, __MLflowTfKeras2Callback
import mlflow.keras
from mlflow.utils.autologging_utils import BatchMetricsLogger, autologging_is_disabled
from unittest.mock import patch

import os

np.random.seed(1337)

SavedModelInfo = collections.namedtuple(
    "SavedModelInfo",
    ["path", "meta_graph_tags", "signature_def_key", "inference_df", "expected_results_df"],
)


@pytest.fixture(autouse=True)
def clear_session():
    yield
    tf.keras.backend.clear_session()


@pytest.fixture
def random_train_data():
    return np.random.random((150, 4))


@pytest.fixture
def random_one_hot_labels():
    n, n_class = (150, 3)
    classes = np.random.randint(0, n_class, n)
    labels = np.zeros((n, n_class))
    labels[np.arange(n), classes] = 1
    return labels


@pytest.fixture
def clear_tf_keras_imports():
    """
    Simulates a state where `tensorflow` and `keras` are not imported by removing these
    libraries from the `sys.modules` dictionary. This is useful for testing the interaction
    between TensorFlow / Keras and the fluent `mlflow.autolog()` API because it will cause import
    hooks to be re-triggered upon re-import after `mlflow.autolog()` is enabled.
    """
    sys.modules.pop("tensorflow", None)
    sys.modules.pop("keras", None)


@pytest.fixture(autouse=True)
def clear_fluent_autologging_import_hooks():
    """
    Clears import hooks for MLflow fluent autologging (`mlflow.autolog()`) between tests
    to ensure that interactions between fluent autologging and TensorFlow / tf.keras can
    be tested successfully
    """
    mlflow.utils.import_hooks._post_import_hooks.pop("tensorflow", None)
    mlflow.utils.import_hooks._post_import_hooks.pop("keras", None)


def create_tf_keras_model():
    model = tf.keras.Sequential()

    model.add(layers.Dense(16, activation="relu", input_shape=(4,)))
    model.add(layers.Dense(3, activation="softmax"))

    model.compile(
        optimizer=tf.keras.optimizers.Adam(), loss="categorical_crossentropy", metrics=["accuracy"]
    )
    return model


@pytest.mark.large
def test_tf_keras_autolog_ends_auto_created_run(random_train_data, random_one_hot_labels):
    mlflow.tensorflow.autolog()

    data = random_train_data
    labels = random_one_hot_labels

    model = create_tf_keras_model()
    model.fit(data, labels, epochs=10)

    assert mlflow.active_run() is None


@pytest.mark.large
@pytest.mark.parametrize("log_models", [True, False])
def test_tf_keras_autolog_log_models_configuration(
    random_train_data, random_one_hot_labels, log_models
):
    # pylint: disable=unused-argument
    mlflow.tensorflow.autolog(log_models=log_models)

    data = random_train_data
    labels = random_one_hot_labels

    model = create_tf_keras_model()

    model.fit(data, labels, epochs=10)

    client = mlflow.tracking.MlflowClient()
    run_id = client.list_run_infos(experiment_id="0")[0].run_id
    artifacts = client.list_artifacts(run_id)
    artifacts = map(lambda x: x.path, artifacts)
    assert ("model" in artifacts) == log_models


@pytest.mark.large
def test_tf_keras_autolog_persists_manually_created_run(random_train_data, random_one_hot_labels):
    mlflow.tensorflow.autolog()
    with mlflow.start_run() as run:
        data = random_train_data
        labels = random_one_hot_labels

        model = create_tf_keras_model()
        model.fit(data, labels, epochs=10)

        assert mlflow.active_run()
        assert mlflow.active_run().info.run_id == run.info.run_id


@pytest.fixture
def tf_keras_random_data_run(random_train_data, random_one_hot_labels, initial_epoch):
    # pylint: disable=unused-argument
    mlflow.tensorflow.autolog()

    data = random_train_data
    labels = random_one_hot_labels

    model = create_tf_keras_model()
    history = model.fit(
        data, labels, epochs=initial_epoch + 10, steps_per_epoch=1, initial_epoch=initial_epoch
    )

    client = mlflow.tracking.MlflowClient()
    return client.get_run(client.list_run_infos(experiment_id="0")[0].run_id), history


@pytest.mark.large
@pytest.mark.parametrize("initial_epoch", [0, 10])
def test_tf_keras_autolog_logs_expected_data(tf_keras_random_data_run):
    run, history = tf_keras_random_data_run
    data = run.data
    assert "accuracy" in data.metrics
    assert "loss" in data.metrics
    # Testing explicitly passed parameters are logged correctly
    assert "epochs" in data.params
    assert data.params["epochs"] == str(history.epoch[-1] + 1)
    assert "steps_per_epoch" in data.params
    assert data.params["steps_per_epoch"] == "1"
    # Testing default parameters are logged correctly
    assert "initial_epoch" in data.params
    assert data.params["initial_epoch"] == str(history.epoch[0])
    # Testing unwanted parameters are not logged
    assert "callbacks" not in data.params
    assert "validation_data" not in data.params
    # Testing optimizer parameters are logged
    assert "opt_name" in data.params
    assert data.params["opt_name"] == "Adam"
    assert "opt_learning_rate" in data.params
    assert "opt_decay" in data.params
    assert "opt_beta_1" in data.params
    assert "opt_beta_2" in data.params
    assert "opt_epsilon" in data.params
    assert "opt_amsgrad" in data.params
    assert data.params["opt_amsgrad"] == "False"
    client = mlflow.tracking.MlflowClient()
    all_epoch_acc = client.get_metric_history(run.info.run_id, "accuracy")
    num_of_epochs = len(history.history["loss"])
    assert len(all_epoch_acc) == num_of_epochs == 10
    artifacts = client.list_artifacts(run.info.run_id)
    artifacts = map(lambda x: x.path, artifacts)
    assert "model_summary.txt" in artifacts


@pytest.mark.large
def test_tf_keras_autolog_records_metrics_for_last_epoch(random_train_data, random_one_hot_labels):
    every_n_iter = 5
    num_training_epochs = 17
    mlflow.tensorflow.autolog(every_n_iter=every_n_iter)

    model = create_tf_keras_model()
    with mlflow.start_run() as run:
        model.fit(
            random_train_data,
            random_one_hot_labels,
            epochs=num_training_epochs,
            initial_epoch=0,
        )

    client = mlflow.tracking.MlflowClient()
    run_metrics = client.get_run(run.info.run_id).data.metrics
    assert "accuracy" in run_metrics
    all_epoch_acc = client.get_metric_history(run.info.run_id, "accuracy")
    assert set([metric.step for metric in all_epoch_acc]) == set([0, 5, 10, 15])


@pytest.mark.large
def test_tf_keras_autolog_logs_metrics_for_single_epoch_training(
    random_train_data, random_one_hot_labels
):
    """
    tf.Keras exhibits inconsistent epoch indexing behavior in comparison with other
    TF2 APIs (e.g., tf.Estimator). tf.Keras uses zero-indexing for epochs,
    while other APIs use one-indexing. Accordingly, this test verifies that metrics are
    produced in the boundary case where a model is trained for a single epoch, ensuring
    that we don't miss the zero index in the tf.Keras case.
    """
    mlflow.tensorflow.autolog(every_n_iter=5)

    model = create_tf_keras_model()
    with mlflow.start_run() as run:
        model.fit(random_train_data, random_one_hot_labels, epochs=1)

    client = mlflow.tracking.MlflowClient()
    run_metrics = client.get_run(run.info.run_id).data.metrics
    assert "accuracy" in run_metrics
    assert "loss" in run_metrics


@pytest.mark.large
def test_tf_keras_autolog_names_positional_parameters_correctly(
    random_train_data, random_one_hot_labels
):
    mlflow.tensorflow.autolog(every_n_iter=5)

    data = random_train_data
    labels = random_one_hot_labels

    model = create_tf_keras_model()

    with mlflow.start_run():
        # Pass `batch_size` as a positional argument for testing purposes
        model.fit(data, labels, 8, epochs=10, steps_per_epoch=1)
        run_id = mlflow.active_run().info.run_id

    client = mlflow.tracking.MlflowClient()
    run_info = client.get_run(run_id)
    assert run_info.data.params.get("batch_size") == "8"


@pytest.mark.large
@pytest.mark.parametrize("initial_epoch", [0, 10])
def test_tf_keras_autolog_model_can_load_from_artifact(tf_keras_random_data_run, random_train_data):
    run, _ = tf_keras_random_data_run

    client = mlflow.tracking.MlflowClient()
    artifacts = client.list_artifacts(run.info.run_id)
    artifacts = map(lambda x: x.path, artifacts)
    assert "model" in artifacts
    assert "tensorboard_logs" in artifacts
    model = mlflow.keras.load_model("runs:/" + run.info.run_id + "/model")
    model.predict(random_train_data)


def get_tf_keras_random_data_run_with_callback(
    random_train_data,
    random_one_hot_labels,
    callback,
    restore_weights,
    patience,
    initial_epoch,
):
    # pylint: disable=unused-argument
    mlflow.tensorflow.autolog(every_n_iter=1)

    data = random_train_data
    labels = random_one_hot_labels

    model = create_tf_keras_model()
    if callback == "early":
        # min_delta is set as such to guarantee early stopping
        callback = tf.keras.callbacks.EarlyStopping(
            monitor="loss",
            patience=patience,
            min_delta=99999999,
            restore_best_weights=restore_weights,
            verbose=1,
        )
    else:

        class CustomCallback(tf.keras.callbacks.Callback):
            def on_train_end(self, logs=None):
                print("Training completed")

        callback = CustomCallback()

    history = model.fit(
        data, labels, epochs=initial_epoch + 10, callbacks=[callback], initial_epoch=initial_epoch
    )

    client = mlflow.tracking.MlflowClient()
    return client.get_run(client.list_run_infos(experiment_id="0")[0].run_id), history, callback


@pytest.fixture
def tf_keras_random_data_run_with_callback(
    random_train_data,
    random_one_hot_labels,
    callback,
    restore_weights,
    patience,
    initial_epoch,
):
    return get_tf_keras_random_data_run_with_callback(
        random_train_data,
        random_one_hot_labels,
        callback,
        restore_weights,
        patience,
        initial_epoch,
    )


@pytest.mark.large
@pytest.mark.parametrize("restore_weights", [True])
@pytest.mark.parametrize("callback", ["early"])
@pytest.mark.parametrize("patience", [0, 1, 5])
@pytest.mark.parametrize("initial_epoch", [0, 10])
def test_tf_keras_autolog_early_stop_logs(tf_keras_random_data_run_with_callback, initial_epoch):
    run, history, callback = tf_keras_random_data_run_with_callback
    metrics = run.data.metrics
    params = run.data.params
    assert "patience" in params
    assert params["patience"] == str(callback.patience)
    assert "monitor" in params
    assert params["monitor"] == "loss"
    assert "verbose" not in params
    assert "mode" not in params
    assert "stopped_epoch" in metrics
    assert "restored_epoch" in metrics
    restored_epoch = int(metrics["restored_epoch"])
    # In this test, the best epoch is always the first epoch because the early stopping callback
    # never observes a loss improvement due to an extremely large `min_delta` value
    assert restored_epoch == initial_epoch
    assert "loss" in history.history
    client = mlflow.tracking.MlflowClient()
    metric_history = client.get_metric_history(run.info.run_id, "loss")
    # Check that MLflow has logged the metrics of the "best" model, in addition to per-epoch metrics
    loss = history.history["loss"]
    assert len(metric_history) == len(loss) + 1
    steps, values = map(list, zip(*[(m.step, m.value) for m in metric_history]))
    # Check that MLflow has logged the correct steps
    assert steps == [*history.epoch, callback.stopped_epoch + 1]
    # Check that MLflow has logged the correct metric values
    np.testing.assert_allclose(values, [*loss, callback.best])


@pytest.mark.large
@pytest.mark.parametrize("restore_weights", [True])
@pytest.mark.parametrize("callback", ["early"])
@pytest.mark.parametrize("patience", [0, 1, 5])
@pytest.mark.parametrize("initial_epoch", [0, 10])
def test_tf_keras_autolog_batch_metrics_logger_logs_expected_metrics(
    callback,
    restore_weights,
    patience,
    initial_epoch,
    random_train_data,
    random_one_hot_labels,
):
    patched_metrics_data = []

    # Mock patching BatchMetricsLogger.record_metrics()
    # to ensure that expected metrics are being logged.
    original = BatchMetricsLogger.record_metrics

    with patch(
        "mlflow.utils.autologging_utils.BatchMetricsLogger.record_metrics", autospec=True
    ) as record_metrics_mock:

        def record_metrics_side_effect(self, metrics, step=None):
            patched_metrics_data.extend(metrics.items())
            original(self, metrics, step)

        record_metrics_mock.side_effect = record_metrics_side_effect
        run, _, callback = get_tf_keras_random_data_run_with_callback(
            random_train_data,
            random_one_hot_labels,
            callback,
            restore_weights,
            patience,
            initial_epoch,
        )
    patched_metrics_data = dict(patched_metrics_data)
    original_metrics = run.data.metrics

    for metric_name in original_metrics:
        assert metric_name in patched_metrics_data

    restored_epoch = int(patched_metrics_data["restored_epoch"])
    assert restored_epoch == initial_epoch


@pytest.mark.large
@pytest.mark.parametrize("restore_weights", [True])
@pytest.mark.parametrize("callback", ["early"])
@pytest.mark.parametrize("patience", [11])
@pytest.mark.parametrize("initial_epoch", [0, 10])
def test_tf_keras_autolog_early_stop_no_stop_does_not_log(tf_keras_random_data_run_with_callback):
    run, history, callback = tf_keras_random_data_run_with_callback
    metrics = run.data.metrics
    params = run.data.params
    assert "patience" in params
    assert params["patience"] == str(callback.patience)
    assert "monitor" in params
    assert params["monitor"] == "loss"
    assert "verbose" not in params
    assert "mode" not in params
    assert "stopped_epoch" not in metrics
    assert "restored_epoch" not in metrics
    assert "loss" in history.history
    num_of_epochs = len(history.history["loss"])
    client = mlflow.tracking.MlflowClient()
    metric_history = client.get_metric_history(run.info.run_id, "loss")
    # Check the test epoch numbers are correct
    assert num_of_epochs == 10
    assert len(metric_history) == num_of_epochs


@pytest.mark.large
@pytest.mark.parametrize("restore_weights", [False])
@pytest.mark.parametrize("callback", ["early"])
@pytest.mark.parametrize("patience", [5])
@pytest.mark.parametrize("initial_epoch", [0, 10])
def test_tf_keras_autolog_early_stop_no_restore_doesnt_log(tf_keras_random_data_run_with_callback):
    run, history, callback = tf_keras_random_data_run_with_callback
    metrics = run.data.metrics
    params = run.data.params
    assert "patience" in params
    assert params["patience"] == str(callback.patience)
    assert "monitor" in params
    assert params["monitor"] == "loss"
    assert "verbose" not in params
    assert "mode" not in params
    assert "stopped_epoch" in metrics
    assert "restored_epoch" not in metrics
    assert "loss" in history.history
    num_of_epochs = len(history.history["loss"])
    client = mlflow.tracking.MlflowClient()
    metric_history = client.get_metric_history(run.info.run_id, "loss")
    # Check the test epoch numbers are correct
    assert num_of_epochs == callback.patience + 1
    assert len(metric_history) == num_of_epochs


@pytest.mark.large
@pytest.mark.parametrize("restore_weights", [False])
@pytest.mark.parametrize("callback", ["not-early"])
@pytest.mark.parametrize("patience", [5])
@pytest.mark.parametrize("initial_epoch", [0, 10])
def test_tf_keras_autolog_non_early_stop_callback_no_log(tf_keras_random_data_run_with_callback):
    run, history = tf_keras_random_data_run_with_callback[:-1]
    metrics = run.data.metrics
    params = run.data.params
    assert "patience" not in params
    assert "monitor" not in params
    assert "verbose" not in params
    assert "mode" not in params
    assert "stopped_epoch" not in metrics
    assert "restored_epoch" not in metrics
    assert "loss" in history.history
    num_of_epochs = len(history.history["loss"])
    client = mlflow.tracking.MlflowClient()
    metric_history = client.get_metric_history(run.info.run_id, "loss")
    # Check the test epoch numbers are correct
    assert num_of_epochs == 10
    assert len(metric_history) == num_of_epochs


@pytest.mark.parametrize("positional", [True, False])
def test_tf_keras_autolog_does_not_mutate_original_callbacks_list(
    tmpdir, random_train_data, random_one_hot_labels, positional
):
    """
    TensorFlow autologging passes new callbacks to the `fit()` / `fit_generator()` function. If
    preexisting user-defined callbacks already exist, these new callbacks are added to the
    user-specified ones. This test verifies that the new callbacks are added to the without
    permanently mutating the original list of callbacks.
    """
    mlflow.tensorflow.autolog()

    tensorboard_callback = tf.keras.callbacks.TensorBoard(log_dir=tmpdir)
    callbacks = [tensorboard_callback]

    model = create_tf_keras_model()
    data = random_train_data
    labels = random_one_hot_labels

    if positional:
        model.fit(data, labels, None, 10, 1, callbacks)
    else:
        model.fit(data, labels, epochs=10, callbacks=callbacks)

    assert len(callbacks) == 1
    assert callbacks == [tensorboard_callback]


@pytest.mark.large
def test_tf_keras_autolog_does_not_delete_logging_directory_for_tensorboard_callback(
    tmpdir, random_train_data, random_one_hot_labels
):
    tensorboard_callback_logging_dir_path = str(tmpdir.mkdir("tb_logs"))
    tensorboard_callback = tf.keras.callbacks.TensorBoard(
        tensorboard_callback_logging_dir_path, histogram_freq=0
    )

    mlflow.tensorflow.autolog()

    data = random_train_data
    labels = random_one_hot_labels

    model = create_tf_keras_model()
    model.fit(data, labels, epochs=10, callbacks=[tensorboard_callback])

    assert os.path.exists(tensorboard_callback_logging_dir_path)


@pytest.mark.large
def test_tf_keras_autolog_logs_to_and_deletes_temporary_directory_when_tensorboard_callback_absent(
    tmpdir, random_train_data, random_one_hot_labels
):
    from unittest import mock
    from mlflow.tensorflow import _TensorBoardLogDir

    mlflow.tensorflow.autolog()

    mock_log_dir_inst = _TensorBoardLogDir(location=str(tmpdir.mkdir("tb_logging")), is_temp=True)
    with mock.patch("mlflow.tensorflow._TensorBoardLogDir", autospec=True) as mock_log_dir_class:
        mock_log_dir_class.return_value = mock_log_dir_inst

        data = random_train_data
        labels = random_one_hot_labels

        model = create_tf_keras_model()
        model.fit(data, labels, epochs=10)

        assert not os.path.exists(mock_log_dir_inst.location)


def create_tf_estimator_model(directory, export, training_steps=100, use_v1_estimator=False):
    CSV_COLUMN_NAMES = ["SepalLength", "SepalWidth", "PetalLength", "PetalWidth", "Species"]

    train = pd.read_csv(
        os.path.join(os.path.dirname(__file__), "iris_training.csv"),
        names=CSV_COLUMN_NAMES,
        header=0,
    )

    train_y = train.pop("Species")

    def input_fn(features, labels, training=True, batch_size=256):
        """An input function for training or evaluating"""
        # Convert the inputs to a Dataset.
        dataset = tf.data.Dataset.from_tensor_slices((dict(features), labels))

        # Shuffle and repeat if you are in training mode.
        if training:
            dataset = dataset.shuffle(1000).repeat()

        return dataset.batch(batch_size)

    my_feature_columns = []
    for key in train.keys():
        my_feature_columns.append(tf.feature_column.numeric_column(key=key))

    feature_spec = {}
    for feature in CSV_COLUMN_NAMES:
        feature_spec[feature] = tf.Variable([], dtype=tf.float64, name=feature)

    receiver_fn = tf.estimator.export.build_raw_serving_input_receiver_fn(feature_spec)

    run_config = tf.estimator.RunConfig(
        # Emit loss metrics to TensorBoard every step
        save_summary_steps=1,
    )

    # If flag set to true, then use the v1 classifier that extends Estimator
    # If flag set to false, then use the v2 classifier that extends EstimatorV2
    if use_v1_estimator:
        classifier = tf.compat.v1.estimator.DNNClassifier(
            feature_columns=my_feature_columns,
            # Two hidden layers of 10 nodes each.
            hidden_units=[30, 10],
            # The model must choose between 3 classes.
            n_classes=3,
            model_dir=directory,
            config=run_config,
        )
    else:
        classifier = tf.estimator.DNNClassifier(
            feature_columns=my_feature_columns,
            # Two hidden layers of 10 nodes each.
            hidden_units=[30, 10],
            # The model must choose between 3 classes.
            n_classes=3,
            model_dir=directory,
            config=run_config,
        )

    classifier.train(input_fn=lambda: input_fn(train, train_y, training=True), steps=training_steps)
    if export:
        classifier.export_saved_model(directory, receiver_fn)


@pytest.mark.large
@pytest.mark.parametrize("export", [True, False])
def test_tf_estimator_autolog_ends_auto_created_run(tmpdir, export):
    directory = tmpdir.mkdir("test")
    mlflow.tensorflow.autolog()
    create_tf_estimator_model(str(directory), export)
    assert mlflow.active_run() is None


@pytest.mark.large
@pytest.mark.parametrize("export", [True, False])
def test_tf_estimator_autolog_persists_manually_created_run(tmpdir, export):
    directory = tmpdir.mkdir("test")
    with mlflow.start_run() as run:
        create_tf_estimator_model(str(directory), export)
        assert mlflow.active_run()
        assert mlflow.active_run().info.run_id == run.info.run_id


@pytest.fixture
def tf_estimator_random_data_run(tmpdir, export):
    # pylint: disable=unused-argument
    directory = tmpdir.mkdir("test")
    mlflow.tensorflow.autolog()
    create_tf_estimator_model(str(directory), export)
    client = mlflow.tracking.MlflowClient()
    return client.get_run(client.list_run_infos(experiment_id="0")[0].run_id)


@pytest.mark.large
@pytest.mark.parametrize("export", [True, False])
@pytest.mark.parametrize("use_v1_estimator", [True, False])
def test_tf_estimator_autolog_logs_metrics(tmpdir, export, use_v1_estimator):
    directory = tmpdir.mkdir("test")
    mlflow.tensorflow.autolog(every_n_iter=5)

    with mlflow.start_run():
        create_tf_estimator_model(
            str(directory), export, use_v1_estimator=use_v1_estimator, training_steps=17
        )
        run_id = mlflow.active_run().info.run_id

    client = mlflow.tracking.MlflowClient()
    run = client.get_run(run_id)

    assert "loss" in run.data.metrics
    assert "steps" in run.data.params
    metrics = client.get_metric_history(run_id, "loss")
    assert set([metric.step for metric in metrics]) == set([1, 6, 11, 16])


@pytest.mark.large
@pytest.mark.parametrize("export", [True])
def test_tf_estimator_v1_autolog_can_load_from_artifact(tmpdir, export):
    directory = tmpdir.mkdir("test")
    mlflow.tensorflow.autolog()

    create_tf_estimator_model(str(directory), export, use_v1_estimator=True)
    client = mlflow.tracking.MlflowClient()
    tf_estimator_v1_run = client.get_run(client.list_run_infos(experiment_id="0")[0].run_id)
    artifacts = client.list_artifacts(tf_estimator_v1_run.info.run_id)
    artifacts = map(lambda x: x.path, artifacts)
    assert "model" in artifacts
    mlflow.tensorflow.load_model("runs:/" + tf_estimator_v1_run.info.run_id + "/model")


@pytest.mark.large
@pytest.mark.parametrize("export", [True, False])
def test_tf_estimator_autolog_logs_tensorboard_logs(tf_estimator_random_data_run):
    client = mlflow.tracking.MlflowClient()
    artifacts = client.list_artifacts(tf_estimator_random_data_run.info.run_id)
    assert any(["tensorboard_logs" in a.path and a.is_dir for a in artifacts])


@pytest.mark.large
def test_tf_estimator_autolog_logs_metrics_in_exclusive_mode(tmpdir):
    mlflow.tensorflow.autolog(exclusive=True)

    create_tf_estimator_model(tmpdir, export=False)
    client = mlflow.tracking.MlflowClient()
    tf_estimator_run = client.get_run(client.list_run_infos(experiment_id="0")[0].run_id)

    assert "loss" in tf_estimator_run.data.metrics
    assert "steps" in tf_estimator_run.data.params
    metrics = client.get_metric_history(tf_estimator_run.info.run_id, "loss")
    assert len(metrics) == 100


@pytest.mark.large
def test_tf_estimator_autolog_logs_metics_for_single_epoch_training(tmpdir):
    """
    Epoch indexing behavior is consistent across TensorFlow 2: tf.Keras uses
    zero-indexing for epochs, while other APIs (e.g., tf.Estimator) use one-indexing.
    This test verifies that metrics are produced for tf.Estimator training sessions
    in the boundary casewhere a model is trained for a single epoch, ensuring that
    we capture metrics from the first epoch at index 1.
    """
    mlflow.tensorflow.autolog()
    with mlflow.start_run() as run:
        create_tf_estimator_model(str(tmpdir), export=False, training_steps=1)
    client = mlflow.tracking.MlflowClient()
    metrics = client.get_metric_history(run.info.run_id, "loss")
    assert len(metrics) == 1
    assert metrics[0].step == 1


@pytest.mark.large
@pytest.mark.parametrize("export", [True])
def test_tf_estimator_autolog_model_can_load_from_artifact(tf_estimator_random_data_run):
    client = mlflow.tracking.MlflowClient()
    artifacts = client.list_artifacts(tf_estimator_random_data_run.info.run_id)
    artifacts = map(lambda x: x.path, artifacts)
    assert "model" in artifacts
    mlflow.tensorflow.load_model("runs:/" + tf_estimator_random_data_run.info.run_id + "/model")


@pytest.mark.large
def test_flush_queue_is_thread_safe():
    """
    Autologging augments TensorBoard event logging hooks with MLflow `log_metric` API
    calls. To prevent these API calls from blocking TensorBoard event logs, `log_metric`
    API calls are scheduled via `_flush_queue` on a background thread. Accordingly, this test
    verifies that `_flush_queue` is thread safe.
    """
    from threading import Thread
    from mlflow.entities import Metric
    from mlflow.tensorflow import _flush_queue, _metric_queue_lock

    client = mlflow.tracking.MlflowClient()
    run = client.create_run(experiment_id="0")
    metric_queue_item = (run.info.run_id, Metric("foo", 0.1, 100, 1))
    mlflow.tensorflow._metric_queue.append(metric_queue_item)

    # Verify that, if another thread holds a lock on the metric queue leveraged by
    # _flush_queue, _flush_queue terminates and does not modify the queue
    _metric_queue_lock.acquire()
    flush_thread1 = Thread(target=_flush_queue)
    flush_thread1.start()
    flush_thread1.join()
    assert len(mlflow.tensorflow._metric_queue) == 1
    assert mlflow.tensorflow._metric_queue[0] == metric_queue_item
    _metric_queue_lock.release()

    # Verify that, if no other thread holds a lock on the metric queue leveraged by
    # _flush_queue, _flush_queue flushes the queue as expected
    flush_thread2 = Thread(target=_flush_queue)
    flush_thread2.start()
    flush_thread2.join()
    assert len(mlflow.tensorflow._metric_queue) == 0


def get_text_vec_model(train_samples):
    # Taken from: https://github.com/mlflow/mlflow/issues/3910

    # pylint: disable=no-name-in-module
    from tensorflow.keras.layers.experimental.preprocessing import TextVectorization

    VOCAB_SIZE = 10
    SEQUENCE_LENGTH = 16
    EMBEDDING_DIM = 16

    vectorizer_layer = TextVectorization(
        input_shape=(1,),
        max_tokens=VOCAB_SIZE,
        output_mode="int",
        output_sequence_length=SEQUENCE_LENGTH,
    )
    vectorizer_layer.adapt(train_samples)
    model = tf.keras.Sequential(
        [
            vectorizer_layer,
            tf.keras.layers.Embedding(
                VOCAB_SIZE,
                EMBEDDING_DIM,
                name="embedding",
                mask_zero=True,
                input_shape=(1,),
            ),
            tf.keras.layers.GlobalAveragePooling1D(),
            tf.keras.layers.Dense(16, activation="relu"),
            tf.keras.layers.Dense(1, activation="tanh"),
        ]
    )
    model.compile(optimizer="adam", loss="mse", metrics="mae")
    return model


@pytest.mark.skipif(
    Version(tf.__version__) < Version("2.3.0"),
    reason=(
        "Deserializing a model with `TextVectorization` and `Embedding`"
        "fails in tensorflow < 2.3.0. See this issue:"
        "https://github.com/tensorflow/tensorflow/issues/38250"
    ),
)
def test_autolog_text_vec_model(tmpdir):
    """
    Verifies autolog successfully saves a model that can't be saved in the H5 format
    """
    mlflow.tensorflow.autolog()

    train_samples = np.array(["this is an example", "another example"])
    train_labels = np.array([0.4, 0.2])
    model = get_text_vec_model(train_samples)

    # Saving in the H5 format should fail
    with pytest.raises(NotImplementedError, match="is not supported in h5"):
        model.save(tmpdir.join("model.h5").strpath, save_format="h5")

    with mlflow.start_run() as run:
        model.fit(train_samples, train_labels, epochs=1)

    loaded_model = mlflow.keras.load_model("runs:/" + run.info.run_id + "/model")
    np.testing.assert_array_equal(loaded_model.predict(train_samples), model.predict(train_samples))


def test_fit_generator(random_train_data, random_one_hot_labels):
    mlflow.tensorflow.autolog()
    model = create_tf_keras_model()

    def generator():
        while True:
            yield random_train_data, random_one_hot_labels

    with mlflow.start_run() as run:
        model.fit_generator(generator(), epochs=10, steps_per_epoch=1)

    run = mlflow.tracking.MlflowClient().get_run(run.info.run_id)
    params = run.data.params
    metrics = run.data.metrics
    assert "epochs" in params
    assert params["epochs"] == "10"
    assert "steps_per_epoch" in params
    assert params["steps_per_epoch"] == "1"
    assert "accuracy" in metrics
    assert "loss" in metrics


@pytest.mark.large
@pytest.mark.usefixtures("clear_tf_keras_imports")
def test_fluent_autolog_with_tf_keras_logs_expected_content(
    random_train_data, random_one_hot_labels
):
    """
    Guards against previously-exhibited issues where using the fluent `mlflow.autolog()` API with
    `tf.keras` Models did not work due to conflicting patches set by both the
    `mlflow.tensorflow.autolog()` and the `mlflow.keras.autolog()` APIs.
    """
    mlflow.autolog()

    model = create_tf_keras_model()

    with mlflow.start_run() as run:
        model.fit(random_train_data, random_one_hot_labels, epochs=10)

    client = mlflow.tracking.MlflowClient()
    run_data = client.get_run(run.info.run_id).data
    assert "accuracy" in run_data.metrics
    assert "epochs" in run_data.params

    artifacts = client.list_artifacts(run.info.run_id)
    artifacts = map(lambda x: x.path, artifacts)
    assert "model" in artifacts


def test_callback_is_picklable():
    cb = __MLflowTfKeras2Callback(
        log_models=True, metrics_logger=BatchMetricsLogger(run_id="1234"), log_every_n_steps=5
    )
    pickle.dumps(cb)

    tb = _TensorBoard()
    pickle.dumps(tb)


@pytest.mark.large
@pytest.mark.skipif(
    Version(tf.__version__) < Version("2.1.0"), reason="This test requires tensorflow >= 2.1.0"
)
def test_tf_keras_autolog_distributed_training(random_train_data, random_one_hot_labels):
    # Ref: https://www.tensorflow.org/tutorials/distribute/keras
    mlflow.tensorflow.autolog()

    with tf.distribute.MirroredStrategy().scope():
        model = create_tf_keras_model()
    fit_params = {"epochs": 10, "batch_size": 10}
    with mlflow.start_run() as run:
        model.fit(random_train_data, random_one_hot_labels, **fit_params)
    client = mlflow.tracking.MlflowClient()
    assert client.get_run(run.info.run_id).data.params.keys() >= fit_params.keys()


@pytest.mark.large
@pytest.mark.skipif(
    Version(tf.__version__) < Version("2.6.0"),
    reason=("TensorFlow only has a hard dependency on Keras in version >= 2.6.0"),
)
@pytest.mark.usefixtures("clear_tf_keras_imports")
def test_fluent_autolog_with_tf_keras_preserves_v2_model_reference():
    """
    Verifies that, in TensorFlow >= 2.6.0, `tensorflow.keras.Model` refers to the correct class in
    the correct module after `mlflow.autolog()` is called, guarding against previously identified
    compatibility issues between recent versions of TensorFlow and MLflow's internal utility for
    setting up autologging import hooks.
    """
    mlflow.autolog()

    import tensorflow.keras
    from keras.api._v2.keras import Model as ModelV2

    assert tensorflow.keras.Model is ModelV2


@pytest.mark.usefixtures("clear_tf_keras_imports")
def test_import_tensorflow_with_fluent_autolog_enables_tf_autologging():
    mlflow.autolog()

    import tensorflow  # pylint: disable=unused-variable,unused-import,reimported

    assert not autologging_is_disabled(mlflow.tensorflow.FLAVOR_NAME)

    # NB: In Tensorflow >= 2.6, we redirect keras autologging to tensorflow autologging
    # so the original keras autologging is disabled
    if Version(tf.__version__) >= Version("2.6"):
        import keras  # pylint: disable=unused-variable,unused-import

        assert autologging_is_disabled(mlflow.keras.FLAVOR_NAME)


@pytest.mark.large
@pytest.mark.usefixtures("clear_tf_keras_imports")
def test_import_tf_keras_with_fluent_autolog_enables_tf_autologging():
    mlflow.autolog()

    import tensorflow.keras  # pylint: disable=unused-variable,unused-import

    assert not autologging_is_disabled(mlflow.tensorflow.FLAVOR_NAME)

    # NB: In Tensorflow >= 2.6, we redirect keras autologging to tensorflow autologging
    # so the original keras autologging is disabled
    if Version(tf.__version__) >= Version("2.6"):
        # NB: For TF >= 2.6, import tensorflow.keras will trigger importing keras
        assert autologging_is_disabled(mlflow.keras.FLAVOR_NAME)


@pytest.mark.large
@pytest.mark.skipif(
    Version(tf.__version__) < Version("2.6.0"),
    reason=("TensorFlow autologging is not used for vanilla Keras models in Keras < 2.6.0"),
)
@pytest.mark.usefixtures("clear_tf_keras_imports")
def test_import_keras_with_fluent_autolog_enables_tensorflow_autologging():
    mlflow.autolog()

    import keras  # pylint: disable=unused-variable,unused-import

    assert not autologging_is_disabled(mlflow.tensorflow.FLAVOR_NAME)
    assert autologging_is_disabled(mlflow.keras.FLAVOR_NAME)
