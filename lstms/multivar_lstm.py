import pandas as pd
import numpy as np

import os
from pathlib import Path
from zipfile import ZipFile
from loguru import logger
from typing import Tuple

from tensorflow import keras

from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import roc_curve, auc

import matplotlib.pyplot as plt


def window_data(dataset: np.ndarray, window_size: int) -> np.ndarray:
    """
    Given a 2-D numpy array and a window size,
    create striding windows over the dataset.

    Args:
    - dataset: a (m, n) shaped numpy array
    - window_size: the number of obervations to window over

    Example:
    dataset = (1003, 20), window_size = 5
    Output = (998, 5, 20)

    Explanation: Starting from the 5th observation, create backwards-looking
    windows of 5 observations each.

    We cannot directly use 1st 4 observations for prediction, since we do not have
    5 previous time points
    """

    # Starting point is 0+window_size
    # Ending point is the last value

    # Create a index matrix
    # Note to DJ: Same approach as for loop + vstack, but vectorized. Idea ripped off from
    # https://towardsdatascience.com/fast-and-robust-sliding-window-vectorization-with-numpy-3ad950ed62f5
    array_idx = (
        0
        + np.expand_dims(np.arange(window_size), 0)
        + np.expand_dims(np.arange(len(dataset) - (window_size - 1)), 0).T
    )

    return dataset[array_idx]

class MultiVarLSTM:

    def __init__(self, raw_data: pd.DataFrame, window_size: int):

        self.raw_data = raw_data
        self.window_size = window_size

    def preprocess(self) -> Tuple[np.ndarray]:
        """
        Standard preprocessing. Define the y variable,
        trim the x variables, train/test split, and min-max scale.

        Returns:
            Scaled and split dataset for modeling.
        """
        logger.info("Starting preprocessing")
        wandering = ((self.raw_data.STEPLENGTH < 680)
                     & (np.abs(self.raw_data.TURNANGLE) > 45))

        # Hold out arbitrary bear as the test set
        test_idx = self.raw_data.loc[self.raw_data.Bear_ID == 79].index

        x_data = self.raw_data.drop(
            columns=['STEPLENGTH', 'TURNANGLE', 'Unnamed: 0', 'datetime'])

        X_train = x_data.drop(test_idx)
        y_train = wandering.drop(test_idx)

        X_test = x_data.loc[test_idx]
        y_test = wandering[test_idx]

        # Strategy for multiple time series: Split up dataset by bear,
        # and iteratively model
        # Preserve indices for training
        X_train = X_train.reset_index(drop=True)
        bear_ids = X_train.Bear_ID.unique()

        self.bear_ids_dict = {
            bearid: X_train.loc[X_train.Bear_ID == bearid].index
            for bearid in bear_ids
        }

        # Fit the minmax scaler
        m = MinMaxScaler()
        m.fit(X_train)
        # Bind to class instance for later back-transformation
        self.scaler = m

        X_train = m.transform(X_train)
        X_test = m.transform(X_test)

        # Return y values as numpy array, not pandas series
        y_train = y_train.values
        y_test = y_test.values

        # Reshape test data, since it's static.
        # Training data needs to be reshaped dynamically since shape is
        # dependent on bear.
        X_test = window_data(X_test, self.window_size)
        y_test = y_test[(self.window_size - 1):]
        return X_train, X_test, y_train, y_test

    def fit_model(self,
                  x_train: np.ndarray,
                  y_train: np.ndarray,
                  x_test: np.ndarray,
                  y_test: np.ndarray) -> keras.models.Sequential:
        """
        Fit the multivariate LSTM.

        Args:
            x_train - the preprocessed training data
            y_train - the boolean classification variable
            x_test - the reshaped testing data
            y_test - the boolean reshaped testing data
        """
        logger.info("Fitting the model")

        lstm_model = keras.models.Sequential()
        lstm_model.add(keras.layers.LSTM(40))
        # lstm_model.add(keras.layers.Attention())
        lstm_model.add(keras.layers.Dense(1))

        lstm_model.compile(optimizer='adam', loss='mse')

        # Fit the model. Iterate by bear ID
        for bear_id, idcs in self.bear_ids_dict.items():

            # Reshape data for sequential model
            logger.info(f"Training on bear {bear_id}")

            bear_x_data = x_train[idcs]
            bear_y_data = y_train[idcs]

            xtrain_reshaped = window_data(bear_x_data, self.window_size)
            # No reshaping of y, all we do is grab
            # every 5th value if the windowsize = 5
            ytrain_reshaped = bear_y_data[(self.window_size - 1):]

            lstm_model.fit(xtrain_reshaped,
                           ytrain_reshaped,
                           validation_data=(x_test, y_test),
                           epochs=20)

        return lstm_model

    def predict_model(self, model, x_test, y_test) -> None:
        """
        Predict out, and get the summary statistics.
        """

        # Raw accuracy
        accuracy = model.evaluate(x_test, y_test, verbose=1)
        logger.info(f"Final accuracy is {accuracy:.2f}")

        # Predicted vs. observed
        y_preds = [1 if x > 0.5 else 0 for x in model.predict(x_test)]
        y_true = [1 if x else 0 for x in y_test]
        fpr, tpr, _ = roc_curve(y_true, y_preds)
        auc_score = auc(fpr, tpr)

        plt.figure()
        plt.plot(fpr, tpr, color='darkorange', lw=2,
                 label='ROC curve (area = %0.2f)' % auc_score)
        plt.plot([0, 1], [0, 1], color='navy', lw=2, linestyle='--')
        plt.xlim([0.0, 1.0])
        plt.ylim([0.0, 1.05])
        plt.xlabel('False Positive Rate')
        plt.ylabel('True Positive Rate')
        plt.title('Receiver operating characteristic example')
        plt.legend(loc="lower right")

        output_path = Path(os.path.abspath(__file__)).parent
        plt.savefig(os.path.join(output_path, 'roc_auc_curve.png'))

    def run(self) -> None:
        """
        Run the LSTM
        """

        x_train, x_test, y_train, y_test = self.preprocess()

        lstm_model = self.fit_model(x_train, y_train, x_test, y_test)
        self.predict_model(lstm_model, x_test, y_test)


def unpack_data() -> pd.DataFrame:
    """
    Unzips the bear data and combines male/female bears into one dataset
    """

    # Directory wrangling
    wd = os.path.abspath(__file__)
    top_level_path = Path(wd).parent.parent
    data_path = Path(top_level_path) / 'data'

    # Extract the male/female data if not already extracted
    if not os.path.exists(data_path):

        zip_path = Path(top_level_path) / 'data.zip'
        logger.info(f"Unpacking data files from {zip_path}")
        with ZipFile(zip_path, 'r') as zipObj:
            zipObj.extractall(top_level_path)

        logger.info(f"Data loaded to {top_level_path / 'data'}")

    # Load datasets
    male_bears = pd.read_csv(Path(data_path) / 'maleclean4.csv')
    female_bears = pd.read_csv(Path(data_path) / 'femaleclean4.csv')

    # Subset to only observed behavior, and concatenate.
    all_bears = pd.concat([male_bears, female_bears], sort=True)

    # Drop misformatted NAs
    all_bears = all_bears.loc[~(all_bears.FID.isna())]
    all_bears.reset_index(inplace=True, drop=True)

    return all_bears


if __name__ == '__main__':
    """
    Runs the multivariate LSTM.

    Idea: use all training variables to predict
    wandering vs. classical behavior

    """

    all_bears = unpack_data()

    # Define pipeline and run
    pipeline = MultiVarLSTM(all_bears, 5)
    pipeline.run()
