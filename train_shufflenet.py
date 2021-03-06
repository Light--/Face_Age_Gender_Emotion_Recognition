import pandas as pd
import logging
import argparse
from pathlib import Path
import numpy as np
from keras.callbacks import LearningRateScheduler, ModelCheckpoint, CSVLogger
from keras.optimizers import SGD, Adam
from keras.utils import np_utils
from my_keras_model import get_opconty_shufflenet_v2, get_mobilenet_v2
from utils.datasets import load_imdb_or_wiki, load_fer2013, split_data
from keras.preprocessing.image import ImageDataGenerator
from utils.image import get_random_eraser
from utils.mixup_generator import MixupGenerator
import keras.backend as K
from sklearn.model_selection import train_test_split
import sys
import keras
import tensorflow as tf
from keras.callbacks import TensorBoard
from sklearn.utils import class_weight
from keras.backend.tensorflow_backend import set_session


logging.basicConfig(level=logging.DEBUG)
image_size = 64
emotion_class_num = 7
small_volume = 0

config = tf.ConfigProto()
# config.gpu_options.per_process_gpu_memory_fraction = 0.1
config.gpu_options.allow_growth = True
set_session(tf.Session(config=config))


def get_args():
    parser = argparse.ArgumentParser(description="This script trains the CNN model for age and gender estimation.",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--input_agender", "-ia", type=str, required=True,
                        help="path to input database mat file of age and gender")
    parser.add_argument("--input_wiki", type=str, required=False,
                        help="path to input database mat file of wiki")
    parser.add_argument("--input_emotion", "-ie", type=str, required=True,
                        help="path to input database file of emotion")
    parser.add_argument("--batch_size", type=int, default=32,
                        help="batch size")
    parser.add_argument("--nb_epochs", type=int, default=30,
                        help="number of epochs")
    parser.add_argument("--lr", type=float, default=0.1,
                        help="initial learning rate")
    parser.add_argument("--opt", type=str, default="sgd",
                        help="optimizer name; 'sgd' or 'adam'")
    parser.add_argument("--depth", type=int, default=16,
                        help="depth of network (should be 10, 16, 22, 28, ...)")
    parser.add_argument("--width", type=int, default=8,
                        help="width of network")
    parser.add_argument("--validation_split", type=float, default=0.1,
                        help="validation split ratio")
    parser.add_argument("--aug", action="store_true",
                        help="use data augmentation if set true")
    parser.add_argument("--output_path", type=str, default="checkpoints",
                        help="checkpoint dir")
    parser.add_argument('--staircase_decay_at_epochs',
                        type=eval, default=(5, 8,))
    args = parser.parse_args()
    return args


class Schedule:
    def __init__(self, nb_epochs, initial_lr):
        self.epochs = nb_epochs
        self.initial_lr = initial_lr

    def __call__(self, epoch_idx):
        if epoch_idx < self.epochs * 0.25:
            return self.initial_lr
        elif epoch_idx < self.epochs * 0.50:
            return self.initial_lr * 0.2
        elif epoch_idx < self.epochs * 0.75:
            return self.initial_lr * 0.04
        return self.initial_lr * 0.008


class NewSchedule:
    def __init__(self, nb_epochs, initial_lr):
        self.epochs = nb_epochs
        self.initial_lr = initial_lr

    def __call__(self, epoch_idx):
        if epoch_idx < self.epochs * 0.25:
            return self.initial_lr
        elif epoch_idx < self.epochs * 0.50:
            return self.initial_lr * 0.1
        elif epoch_idx < self.epochs * 0.75:
            return self.initial_lr * 0.1
        return self.initial_lr * 0.1


class StaircaseSchedule:
    def __init__(self, staircase_decay_at_epochs, initial_lr):
        self.staircase_decay_at_epochs = staircase_decay_at_epochs
        self.initial_lr = initial_lr

    def __call__(self, epoch_idx):
        decay_idx = -1
        for i, decay_at_epoch in enumerate(self.staircase_decay_at_epochs):
            if (epoch_idx+1) == decay_at_epoch:
                decay_idx = i
        lr = self.initial_lr * 0.1 ** (decay_idx + 1)
        print('lr is adjusted as {} at {}-th epoch.'.format(lr, epoch_idx + 1))
        return lr


def get_optimizer(opt_name, lr):
    if opt_name == "sgd":
        return SGD(lr=lr, momentum=0.9, nesterov=True)
    elif opt_name == "adam":
        return Adam(lr=lr)
    else:
        raise ValueError("optimizer name should be 'sgd' or 'adam'")


def myloss(y_true, y_pred):
    x = keras.losses.categorical_crossentropy(y_true, y_pred)
    # x = K.switch(K.equal(y_true[0], tf.constant(-1.0))[0], tf.constant(0.0),
    #              keras.losses.categorical_crossentropy(y_true, y_pred))
    # x = K.switch(K.equal(y_true[0], tf.constant(-1.0))[0], tf.constant(0.0),
    #              -K.categorical_crossentropy(y_true, y_pred))
    # x = K.switch(K.equal(tf.cast(y_true[0], tf.int8), tf.constant(-1, dtype=tf.int8))[0], tf.constant(0.0),
    #              K.categorical_crossentropy(y_true, y_pred))
    return K.mean(x)
    # return K.sum(x)


def sample_generator(imdb_train, fer_train, batch_size=32):
    imdb_imgs_train, imdb_genders_train, imdb_ages_train, imdb_emotions_train = imdb_train
    fer_imgs_train, fer_genders_train, fer_ages_train, fer_emotions_train = fer_train
    imdb_sample_num = len(imdb_imgs_train)
    fer_sample_num = len(fer_imgs_train)

    while True:
        selected_imdb_idx = np.random.choice(imdb_sample_num, int(batch_size / 2))
        selected_fer_idx = np.random.choice(fer_sample_num, int(batch_size / 2))
        selected_imdb_imgs = imdb_imgs_train[selected_imdb_idx]
        selected_imdb_genders = imdb_genders_train[selected_imdb_idx]
        selected_imdb_ages = imdb_ages_train[selected_imdb_idx]
        selected_imdb_emotions = imdb_emotions_train[selected_imdb_idx]
        selected_fer_imgs = fer_imgs_train[selected_fer_idx]
        selected_fer_genders = fer_genders_train[selected_fer_idx]
        selected_fer_ages = fer_ages_train[selected_fer_idx]
        selected_fer_emotions = fer_emotions_train[selected_fer_idx]
        X_train = np.vstack((selected_imdb_imgs, selected_fer_imgs))
        y_genders_train = np.vstack((selected_imdb_genders, selected_fer_genders))
        y_ages_train = np.vstack((selected_imdb_ages, selected_fer_ages))
        y_emotions_train = np.vstack((selected_imdb_emotions, selected_fer_emotions))
        yield X_train, [y_genders_train, y_ages_train, y_emotions_train]


def myMAE(y_true, y_pred):
    pred_age = K.flatten(K.dot(y_pred, K.reshape(K.arange(0.0, 101.0), (101, 1))))[0]
    label_age = K.cast(K.argmax(y_true), dtype=tf.float32)
    return K.abs(pred_age - label_age)


def load_wiki(input_wiki_path):
    wiki_imgs, wiki_genders, wiki_ages, _, _, _ = load_imdb_or_wiki(input_wiki_path)
    if small_volume != 0:
        wiki_imgs = wiki_imgs[0:small_volume]
        wiki_genders = wiki_genders[0:small_volume]
        wiki_ages = wiki_ages[0:small_volume]
    wiki_genders = np_utils.to_categorical(wiki_genders, 2)
    wiki_ages = np_utils.to_categorical(wiki_ages, 101)
    wiki_emotions = np.full((len(wiki_imgs), emotion_class_num), 0)
    # wiki_emotions = np.full((len(wiki_imgs), emotion_class_num), -1, dtype=np.int8)
    print('wiki_imgs.shape: {}, wiki_genders.shape: {}, wiki_ages.shape: {}, wiki_emotions.shape: {}'.format(
        wiki_imgs.shape, wiki_genders.shape,
        wiki_ages.shape,
        wiki_emotions.shape))

    return wiki_imgs, wiki_genders, wiki_ages, wiki_emotions


def main():
    args = get_args()
    input_agender_path = args.input_agender
    input_emotion_path = args.input_emotion
    input_wiki_path = args.input_wiki
    batch_size = args.batch_size
    nb_epochs = args.nb_epochs
    lr = args.lr
    staircase_decay_at_epochs = args.staircase_decay_at_epochs
    opt_name = args.opt
    depth = args.depth
    k = args.width
    validation_split = args.validation_split
    use_augmentation = args.aug
    output_path = Path(__file__).resolve().parent.joinpath(args.output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    logging.debug("Loading data...")
    # load imdb: 171852 images
    imdb_imgs, imdb_genders, imdb_ages, _, _, _ = load_imdb_or_wiki(input_agender_path)
    # gender_class_weight = class_weight.compute_class_weight('balanced', np.unique(imdb_genders), imdb_genders)
    # age_class_weight = class_weight.compute_class_weight('balanced', np.unique(imdb_ages), imdb_ages)
    if small_volume != 0:
        imdb_imgs = imdb_imgs[0:small_volume]
        imdb_genders = imdb_genders[0:small_volume]
        imdb_ages = imdb_ages[0:small_volume]
    imdb_genders = np_utils.to_categorical(imdb_genders, 2)
    imdb_ages = np_utils.to_categorical(imdb_ages, 101)
    imdb_emotions = np.full((len(imdb_imgs), emotion_class_num), 0)
    # imdb_emotions = np.full((len(imdb_imgs), emotion_class_num), -1, dtype=np.int8)
    print('imdb_imgs.shape: {}, imdb_genders.shape: {}, imdb_ages.shape: {}, imdb_emotions.shape: {}'.format(
        imdb_imgs.shape, imdb_genders.shape,
        imdb_ages.shape,
        imdb_emotions.shape))

    if input_wiki_path:
        # loadk wiki: 38138 images
        wiki_imgs, wiki_genders, wiki_ages, wiki_emotions = load_wiki(input_wiki_path)

    # load fer2013: 35887 images
    fer_imgs, fer_emotions = load_fer2013(input_emotion_path, resize=(image_size, image_size))
    # emotion_class_weight = class_weight.compute_class_weight('balanced', np.unique(fer_emotions), fer_emotions)
    if small_volume != 0:
        fer_imgs = fer_imgs[0:small_volume]
        fer_emotions = fer_emotions[0:small_volume]
    fer_imgs = np.squeeze(np.stack((fer_imgs,) * 3, -1))  # convert gray into color
    fer_emotions = pd.get_dummies(fer_emotions).as_matrix()
    fer_genders = np.full((len(fer_imgs), 2), 0)
    fer_ages = np.full((len(fer_imgs), 101), 0)
    # fer_genders = np.full((len(fer_imgs), 2), -1, dtype=np.int8)
    # fer_ages = np.full((len(fer_imgs), 101), -1, dtype=np.int8)
    print('fer_imgs.shape: {}, fer_genders.shape: {}, fer_ages.shape: {}, fer_emotions.shape: {}'.format(
        fer_imgs.shape, fer_genders.shape,
        fer_ages.shape,
        fer_emotions.shape))

    logging.debug("Splitting data...")
    # split imdb into train and validate set
    imdb_imgs_train, imdb_imgs_val, imdb_genders_train, imdb_genders_val, imdb_ages_train, imdb_ages_val, imdb_emotions_train, imdb_emotions_val \
        = train_test_split(
        imdb_imgs,
        imdb_genders,
        imdb_ages,
        imdb_emotions,
        test_size=validation_split,
        shuffle=False)
    print(
        'imdb_imgs_train.shape: {}, imdb_imgs_val.shape: {}, imdb_genders_train.shape: {}, imdb_genders_val.shape: {}, imdb_ages_train.shape: {} \
        , imdb_ages_val.shape: {}, imdb_emotions_train.shape: {}, imdb_emotions_val.shape: {}'.format(
            imdb_imgs_train.shape,
            imdb_imgs_val.shape,
            imdb_genders_train.shape,
            imdb_genders_val.shape, imdb_ages_train.shape,
            imdb_ages_val.shape, imdb_emotions_train.shape, imdb_emotions_val.shape))

    if input_wiki_path:
        # split wiki into train and validate set
        wiki_imgs_train, wiki_imgs_val, wiki_genders_train, wiki_genders_val, wiki_ages_train, wiki_ages_val, wiki_emotions_train, wiki_emotions_val \
            = train_test_split(
            wiki_imgs,
            wiki_genders,
            wiki_ages,
            wiki_emotions,
            test_size=validation_split,
            shuffle=False)
        print(
            'wiki_imgs_train.shape: {}, wiki_imgs_val.shape: {}, wiki_genders_train.shape: {}, wiki_genders_val.shape: {}, wiki_ages_train.shape: {} \
            , wiki_ages_val.shape: {}, wiki_emotions_train.shape: {}, wiki_emotions_val.shape: {}'.format(
                wiki_imgs_train.shape,
                wiki_imgs_val.shape,
                wiki_genders_train.shape,
                wiki_genders_val.shape, wiki_ages_train.shape,
                wiki_ages_val.shape, wiki_emotions_train.shape, wiki_emotions_val.shape))
    
    # split fer2013 into train and validate set
    fer_imgs_train, fer_imgs_val, fer_genders_train, fer_genders_val, fer_ages_train, fer_ages_val, fer_emotions_train, fer_emotions_val \
        = train_test_split(
        fer_imgs,
        fer_genders,
        fer_ages,
        fer_emotions,
        test_size=validation_split,
        shuffle=False)
    print(
        'fer_imgs_train.shape: {}, fer_imgs_val.shape: {}, fer_genders_train.shape: {}, fer_genders_val.shape: {}, fer_ages_train.shape: {} \
        , fer_ages_val.shape: {}, fer_emotions_train.shape: {}, fer_emotions_val.shape: {}'.format(
            fer_imgs_train.shape,
            fer_imgs_val.shape,
            fer_genders_train.shape,
            fer_genders_val.shape, fer_ages_train.shape,
            fer_ages_val.shape, fer_emotions_train.shape, fer_emotions_val.shape))

    # merge imdb and fer2013 validate set
    logging.debug("Merge validation set...")
    X_val = np.vstack((imdb_imgs_val, fer_imgs_val))
    y_genders_val = np.vstack((imdb_genders_val, fer_genders_val))
    y_ages_val = np.vstack((imdb_ages_val, fer_ages_val))
    y_emotions_val = np.vstack((imdb_emotions_val, fer_emotions_val))
    # merge wiki validate set with above
    if input_wiki_path:
        X_val = np.vstack((X_val, wiki_imgs_val))
        y_genders_val = np.vstack((y_genders_val, wiki_genders_val))
        y_ages_val = np.vstack((y_ages_val, wiki_ages_val))
        y_emotions_val = np.vstack((y_emotions_val, wiki_emotions_val))

    # model = get_mobilenet_v2()
    model = get_opconty_shufflenet_v2()
    opt = get_optimizer(opt_name, lr)
    # model.compile(optimizer=opt,
    #               loss=[myloss, myloss, myloss],
    #               metrics=['accuracy'])
    model.compile(optimizer=opt,
                  loss={'output_gender': 'categorical_crossentropy', 'output_age': 'categorical_crossentropy',
                        'output_emotion': 'categorical_crossentropy'},
                  metrics={'output_gender': 'accuracy', 'output_age': myMAE,
                           'output_emotion': 'accuracy'})

    logging.debug("Model summary...")
    # model.count_params()
    model.summary()
    # print(model.get_layer(name='conv2d_1').kernel_regularizer)

    # lr_schedule = LearningRateScheduler(schedule=StaircaseSchedule(staircase_decay_at_epochs, lr))
    lr_schedule = LearningRateScheduler(schedule=NewSchedule(nb_epochs, lr))
    callbacks = [lr_schedule,
                 ModelCheckpoint(str(output_path) + "/weights.{epoch:02d}-{val_loss:.2f}.hdf5",
                                 monitor="val_loss",
                                 verbose=1,
                                 save_best_only=True,
                                 mode="auto"),
                 TensorBoard(log_dir='./logs', write_graph=True, write_images=True),
                 CSVLogger('logs/train_log.csv', append=False)
                 ]

    logging.debug("Running training...")

    # if use_augmentation:
    # datagen_agender = ImageDataGenerator(
    #     width_shift_range=0.1,
    #     height_shift_range=0.1,
    #     horizontal_flip=True,
    #     preprocessing_function=get_random_eraser(v_l=0, v_h=255))
    # datagen_emotion = ImageDataGenerator(
    #     featurewise_center=False,
    #     featurewise_std_normalization=False,
    #     rotation_range=10,
    #     width_shift_range=0.1,
    #     height_shift_range=0.1,
    #     zoom_range=.1,
    #     horizontal_flip=True)
    # datagen_agender = ImageDataGenerator(
    #     featurewise_center=False,
    #     featurewise_std_normalization=False,
    #     rotation_range=10,
    #     width_shift_range=0.1,
    #     height_shift_range=0.1,
    #     zoom_range=.1,
    #     horizontal_flip=True,
    #     preprocessing_function=get_random_eraser(v_l=0, v_h=255))
    # training_generator = MixupGenerator(X_train, [y_genders_train, y_ages_train, y_emotions_train],
    #                                     batch_size=batch_size,
    #                                     alpha=0.2,
    #                                     datagen=datagen_agender)()
    # else:
    # hist = model.fit(X_train, [y_genders_train, y_ages_train, y_emotions_train], batch_size=batch_size,
    #                  epochs=nb_epochs,
    #                  callbacks=callbacks,
    #                  validation_data=(X_val, [y_genders_val, y_ages_val, y_emotions_val]))

    # fit_class_weight = {'output_gender': dict(enumerate(gender_class_weight)),
    #                     'output_age': dict(enumerate(age_class_weight)),
    #                     'output_emotion': dict(enumerate(emotion_class_weight))}

    if input_wiki_path:
        train_length = len(imdb_imgs_train) + len(fer_imgs_train) + len(wiki_imgs_train)
    else:
        train_length = len(imdb_imgs_train) + len(fer_imgs_train)
    hist = model.fit_generator(
        generator=sample_generator((imdb_imgs_train, imdb_genders_train, imdb_ages_train, imdb_emotions_train),
                                   (fer_imgs_train, fer_genders_train, fer_ages_train, fer_emotions_train),
                                   batch_size=32),
        steps_per_epoch=train_length // batch_size,
        validation_data=(X_val, [y_genders_val, y_ages_val, y_emotions_val]),
        epochs=nb_epochs, verbose=1,
        callbacks=callbacks)

    model.save(str(output_path) + '/model.h5')
    logging.debug("Saving history...")
    pd.DataFrame(hist.history).to_hdf(output_path.joinpath("history_{}_{}.h5".format(depth, k)), "history")


import datetime

if __name__ == '__main__':
    start_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    main()
    end_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(
        'start_time: {}, end_time: {}'.format(start_time, end_time))
