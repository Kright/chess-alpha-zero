"""
Defines the actual model for making policy and value predictions given an observation.
"""

import ftplib
import hashlib
import json
import os
from logging import getLogger

from keras.layers import LeakyReLU, Concatenate
from keras.engine.topology import Input
from keras.engine.training import Model
from keras.layers.convolutional import Conv2D, SeparableConv2D
from keras.layers.core import Activation, Dense, Flatten
from keras.layers.merge import Add
from keras.layers.normalization import BatchNormalization
from keras.regularizers import l2

from chess_zero.agent.api_chess import ChessModelAPI
from chess_zero.config import Config
from typing import Tuple

# noinspection PyPep8Naming

logger = getLogger(__name__)


class ChessModel:
    """
    The model which can be trained to take observations of a game of chess and return value and policy
    predictions.

    Attributes:
        :ivar Config config: configuration to use
        :ivar Model model: the Keras model to use for predictions
        :ivar digest: basically just a hash of the file containing the weights being used by this model
        :ivar ChessModelAPI api: the api to use to listen for and then return this models predictions (on a pipe).
    """

    def __init__(self, config: Config):
        self.config: Config = config
        self.model: Model = None
        self.digest = None
        self.api = None

    def get_pipes(self, num=1):
        """
        Creates a list of pipes on which observations of the game state will be listened for. Whenever
        an observation comes in, returns policy and value network predictions on that pipe.

        :param int num: number of pipes to create
        :return str(Connection): a list of all connections to the pipes that were created
        """
        if self.api is None:
            self.api = ChessModelAPI(self)
            self.api.start()
        return [self.api.create_pipe() for _ in range(num)]

    def build(self):
        """
        Builds the full Keras model and stores it in self.model.
        """
        mc = self.config.model
        in_x = x = Input((18, 8, 8))

        # (batch, channels, height, width)
        x = Conv2D(filters=mc.cnn_filter_num, kernel_size=mc.cnn_first_filter_size, padding="same",
                   data_format="channels_first", use_bias=False, kernel_regularizer=l2(mc.l2_reg),
                   name="input_conv-" + str(mc.cnn_first_filter_size) + "-" + str(mc.cnn_filter_num))(x)
        x = BatchNormalization(axis=1, name="input_batchnorm")(x)
        x = Activation("relu", name="input_relu")(x)

        for i in range(mc.res_layer_num):
            x = self._build_residual_block(x, i + 1)

        res_out = x

        # for policy output
        x = Conv2D(filters=2, kernel_size=1, data_format="channels_first", use_bias=False,
                   kernel_regularizer=l2(mc.l2_reg),
                   name="policy_conv-1-2")(res_out)
        x = BatchNormalization(axis=1, name="policy_batchnorm")(x)
        x = Activation("relu", name="policy_relu")(x)
        x = Flatten(name="policy_flatten")(x)
        # no output for 'pass'
        policy_out = Dense(self.config.n_labels, kernel_regularizer=l2(mc.l2_reg), activation="softmax",
                           name="policy_out")(x)

        # for value output
        x = Conv2D(filters=4, kernel_size=1, data_format="channels_first", use_bias=False,
                   kernel_regularizer=l2(mc.l2_reg),
                   name="value_conv-1-4")(res_out)
        x = BatchNormalization(axis=1, name="value_batchnorm")(x)
        x = Activation("relu", name="value_relu")(x)
        x = Flatten(name="value_flatten")(x)
        x = Dense(mc.value_fc_size, kernel_regularizer=l2(mc.l2_reg), activation="relu", name="value_dense")(x)
        value_out = Dense(1, kernel_regularizer=l2(mc.l2_reg), activation="tanh", name="value_out")(x)

        self.model = Model(in_x, [policy_out, value_out], name="chess_model")

    def _build_residual_block(self, x, index):
        mc = self.config.model
        in_x = x
        res_name = "res" + str(index)
        x = Conv2D(filters=mc.cnn_filter_num, kernel_size=mc.cnn_filter_size, padding="same",
                   data_format="channels_first", use_bias=False, kernel_regularizer=l2(mc.l2_reg),
                   name=res_name + "_conv1-" + str(mc.cnn_filter_size) + "-" + str(mc.cnn_filter_num))(x)
        x = BatchNormalization(axis=1, name=res_name + "_batchnorm1")(x)
        x = Activation("relu", name=res_name + "_relu1")(x)
        x = Conv2D(filters=mc.cnn_filter_num, kernel_size=mc.cnn_filter_size, padding="same",
                   data_format="channels_first", use_bias=False, kernel_regularizer=l2(mc.l2_reg),
                   name=res_name + "_conv2-" + str(mc.cnn_filter_size) + "-" + str(mc.cnn_filter_num))(x)
        x = BatchNormalization(axis=1, name="res" + str(index) + "_batchnorm2")(x)
        x = Add(name=res_name + "_add")([in_x, x])
        x = Activation("relu", name=res_name + "_relu2")(x)
        return x

    @staticmethod
    def fetch_digest(weight_path):
        if os.path.exists(weight_path):
            m = hashlib.sha256()
            with open(weight_path, "rb") as f:
                m.update(f.read())
            return m.hexdigest()

    def load(self, config_path, weight_path):
        """

        :param str config_path: path to the file containing the entire configuration
        :param str weight_path: path to the file containing the model weights
        :return: true iff successful in loading
        """
        mc = self.config.model
        resources = self.config.resource
        if mc.distributed and config_path == resources.model_best_config_path:
            try:
                logger.debug("loading model from server")
                ftp_connection = ftplib.FTP(resources.model_best_distributed_ftp_server,
                                            resources.model_best_distributed_ftp_user,
                                            resources.model_best_distributed_ftp_password)
                ftp_connection.cwd(resources.model_best_distributed_ftp_remote_path)
                ftp_connection.retrbinary("RETR model_best_config.json", open(config_path, 'wb').write)
                ftp_connection.retrbinary("RETR model_best_weight.h5", open(weight_path, 'wb').write)
                ftp_connection.quit()
            except:
                pass
        if os.path.exists(config_path) and os.path.exists(weight_path):
            logger.debug(f"loading model from {config_path}")
            with open(config_path, "rt") as f:
                self.model = Model.from_config(json.load(f))
            self.model.load_weights(weight_path)
            self.model._make_predict_function()
            self.digest = self.fetch_digest(weight_path)
            logger.debug(f"loaded model digest = {self.digest}")
            return True
        else:
            logger.debug(f"model files does not exist at {config_path} and {weight_path}")
            return False

    def save(self, config_path, weight_path):
        """

        :param str config_path: path to save the entire configuration to
        :param str weight_path: path to save the model weights to
        """
        logger.debug(f"save model to {config_path}")
        with open(config_path, "wt") as f:
            json.dump(self.model.get_config(), f)
            self.model.save_weights(weight_path)
        self.digest = self.fetch_digest(weight_path)
        logger.debug(f"saved model digest {self.digest}")

        mc = self.config.model
        resources = self.config.resource
        if mc.distributed and config_path == resources.model_best_config_path:
            try:
                logger.debug("saving model to server")
                ftp_connection = ftplib.FTP(resources.model_best_distributed_ftp_server,
                                            resources.model_best_distributed_ftp_user,
                                            resources.model_best_distributed_ftp_password)
                ftp_connection.cwd(resources.model_best_distributed_ftp_remote_path)
                fh = open(config_path, 'rb')
                ftp_connection.storbinary('STOR model_best_config.json', fh)
                fh.close()

                fh = open(weight_path, 'rb')
                ftp_connection.storbinary('STOR model_best_weight.h5', fh)
                fh.close()
                ftp_connection.quit()
            except:
                pass


class BetterChessModel(ChessModel):
    def __init__(self, config: Config):
        super().__init__(config)

        self.data_format: str = "channels_first"
        self.blocks_count: int = 6
        self.channels: int = 128
        self.conv_along_axis_channels: int = 16
        self.conv15_channels: int = 4
        self.conv5_channels = self.channels - self.conv_along_axis_channels * 2 - self.conv15_channels
        self.leaky_alpha = 0.3

    def build(self):
        mc = self.config.model

        in_x = x = Input((18, 8, 8))
        x = self.pointwise_conv(x, self.channels)

        for i in range(self.blocks_count):
            x = self.custom_layer(x)

        res_out = x

        # for policy output
        x = self.pointwise_conv(res_out, 2)
        x = Flatten(name="policy_flatten")(x)
        policy_out = Dense(self.config.n_labels, activation="softmax", name="policy_out")(x)

        # for value output
        x = self.pointwise_conv(res_out, 4)

        x = Flatten(name="value_flatten")(x)
        x = Dense(mc.value_fc_size, name="value_dense")(x)
        x = LeakyReLU(self.leaky_alpha)(x)
        value_out = Dense(1, activation="tanh", name="value_out")(x)

        self.model = Model(in_x, [policy_out, value_out], name="chess_model")

    def pointwise_conv(self, layer, channels: int):
        layer = Conv2D(channels, 1, data_format=self.data_format, padding="same", use_bias=False)(layer)
        layer = BatchNormalization(axis=1)(layer)
        return LeakyReLU(alpha=self.leaky_alpha)(layer)

    def separable_conv(self, layer, channels, kernel):
        layer = SeparableConv2D(channels, kernel, data_format=self.data_format, padding="same", use_bias=False)(layer)
        layer = BatchNormalization(axis=1)(layer)
        return LeakyReLU(self.leaky_alpha)(layer)

    def pointwise_and_separable(self, layer, channels: int, kernel: Tuple[int, int]):
        layer = self.pointwise_conv(layer, channels)(layer)
        return self.separable_conv(layer, channels, kernel)(layer)

    def custom_layer(self, previous):
        conv5 = self.separable_conv(previous, self.conv5_channels, (5, 5))
        conv_along1 = self.pointwise_and_separable(previous, self.conv_along_axis_channels, (1, 15))
        conv_along2 = self.pointwise_and_separable(previous, self.conv_along_axis_channels, (15, 1))
        conv15 = self.pointwise_and_separable(previous, self.conv15_channels, (15, 15))

        return Concatenate(axis=1)([conv5, conv15, conv_along1, conv_along2])
