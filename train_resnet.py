"""
Trains a modified Resnet to generate approximate dlatents using examples from a trained StyleGAN.
Props to @SimJeg on GitHub for the original code this is based on, from this thread: https://github.com/Puzer/stylegan-encoder/issues/1#issuecomment-490469454
"""
import os
import math
import numpy as np
import pickle
import cv2
import argparse

import dnnlib
import config
import dnnlib.tflib as tflib

import tensorflow
import keras
import keras.backend as K

from keras_applications.resnet_v2 import ResNet50V2, ResNet101V2, ResNet152V2, preprocess_input
from keras.layers import Input, LocallyConnected1D, Reshape, Permute, Conv2D, Add
from keras.models import Model, load_model

def generate_dataset_main(n=10000, save_path=None, seed=None, model_res=1024, image_size=256, minibatch_size=32):
    """
    Generates a dataset of 'n' images of shape ('size', 'size', 3) with random seed 'seed'
    along with their dlatent vectors W of shape ('n', 512)

    These datasets can serve to train an inverse mapping from X to W as well as explore the latent space

    More variation added to latents; also, negative truncation added to balance these examples.
    """

    n = n // 2 # this gets doubled because of negative truncation below
    model_scale = int(2*(math.log(model_res,2)-1)) # For example, 1024 -> 18

    Gs = load_Gs()
    if (model_scale % 3 == 0):
        mod_l = 3
    else:
        mod_l = 2
    if seed is not None:
        b = bool(np.random.RandomState(seed).randint(2))
        Z = np.random.RandomState(seed).randn(n*mod_l, Gs.input_shape[1])
    else:
        b = bool(np.random.randint(2))
        Z = np.random.randn(n*mod_l, Gs.input_shape[1])
    if b:
        mod_l = model_scale // 2
    mod_r = model_scale // mod_l
    if seed is not None:
        Z = np.random.RandomState(seed).randn(n*mod_l, Gs.input_shape[1])
    else:
        Z = np.random.randn(n*mod_l, Gs.input_shape[1])
    W = Gs.components.mapping.run(Z, None, minibatch_size=minibatch_size) # Use mapping network to get unique dlatents for more variation.
    dlatent_avg = Gs.get_var('dlatent_avg') # [component]
    W = (W[np.newaxis] - dlatent_avg) * np.reshape([1, -1], [-1, 1, 1, 1]) + dlatent_avg # truncation trick and add negative image pair
    W = np.append(W[0], W[1], axis=0)
    W = W[:, :mod_r]
    W = W.reshape((n*2, model_scale, 512))
    X = Gs.components.synthesis.run(W, randomize_noise=False, minibatch_size=minibatch_size, print_progress=True,
                                    output_transform=dict(func=tflib.convert_images_to_uint8, nchw_to_nhwc=True))
    X = np.array([cv2.resize(x, (image_size, image_size), interpolation = cv2.INTER_AREA) for x in X])
    X = preprocess_input(X, backend = keras.backend, layers = keras.layers, models = keras.models, utils = keras.utils)
    return W, X

def generate_dataset(n=10000, save_path=None, seed=None, model_res=1024, image_size=256, minibatch_size=16):
    """
    Use generate_dataset_main() as a helper function.
    Divides requests into batches to save memory.
    """
    batch_size = 16
    inc = n//batch_size
    left = n-((batch_size-1)*inc)
    W, X = generate_dataset_main(inc, save_path, seed, model_res, image_size, minibatch_size)
    for i in range(batch_size-2):
        aW, aX = generate_dataset_main(inc, save_path, seed, model_res, image_size, minibatch_size)
        W = np.append(W, aW, axis=0)
        aW = None
        X = np.append(X, aX, axis=0)
        aX = None
    aW, aX = generate_dataset_main(left, save_path, seed, model_res, image_size, minibatch_size)
    W = np.append(W, aW, axis=0)
    aW = None
    X = np.append(X, aX, axis=0)
    aX = None

    if save_path is not None:
        prefix = '_{}_{}'.format(seed, n)
        np.save(os.path.join(os.path.join(save_path, 'W' + prefix)), W)
        np.save(os.path.join(os.path.join(save_path, 'X' + prefix)), X)

    return W, X

def is_square(n):
  return (n == int(math.sqrt(n) + 0.5)**2)
  
def get_resnet_model(save_path, model_res=1024, image_size=256, depth=2, size=0, activation='elu'):
    # Build model
    if os.path.exists(save_path):
        print('Loading model')
        return load_model(save_path)

    print('Building model')
    model_scale = int(2*(math.log(model_res,2)-1)) # For example, 1024 -> 18
    if size <= 0:
        resnet = ResNet50V2(include_top=False, pooling=None, weights='imagenet', input_shape=(image_size, image_size, 3), backend = keras.backend, layers = keras.layers, models = keras.models, utils = keras.utils)
    if size == 1:
        resnet = ResNet101V2(include_top=False, pooling=None, weights='imagenet', input_shape=(image_size, image_size, 3), backend = keras.backend, layers = keras.layers, models = keras.models, utils = keras.utils)
    if size >= 2:
        resnet = ResNet152V2(include_top=False, pooling=None, weights='imagenet', input_shape=(image_size, image_size, 3), backend = keras.backend, layers = keras.layers, models = keras.models, utils = keras.utils)

    layer_size = model_scale*8*8*8
    if is_square(layer_size): # work out layer dimensions
        layer_l = int(math.sqrt(layer_size)+0.5)
        layer_r = layer_l
    else:
        layer_m = math.log(math.sqrt(layer_size),2)
        layer_l = 2**math.ceil(layer_m)
        layer_r = layer_size // layer_l
    layer_l = int(layer_l)
    layer_r = int(layer_r)

    x_init = None
    inp = Input(shape=(image_size, image_size, 3))
    x = resnet(inp)

    if (depth < 0):
        depth = 1

    if (size < 1):
        x = Conv2D(model_scale*8*4, 1, activation=activation)(x) # scale down a bit
        x = Reshape((layer_r*2, layer_l*2))(x) # See https://github.com/OliverRichter/TreeConnect/blob/master/cifar.py - TreeConnect inspired layers instead of dense layers.
    else:
        if (size == 1):
            x = Conv2D(1024, 1, activation=activation)(x) # scale down
            x = Reshape((256, 256))(x) # See https://github.com/OliverRichter/TreeConnect/blob/master/cifar.py - TreeConnect inspired layers instead of dense layers.
        else:
            x = Reshape((256, 512))(x) # See https://github.com/OliverRichter/TreeConnect/blob/master/cifar.py - TreeConnect inspired layers instead of dense layers.

    while (depth > 0):
        x = LocallyConnected1D(layer_r, 1, activation=activation)(x)
        x = Permute((2, 1))(x)
        x = LocallyConnected1D(layer_l, 1, activation=activation)(x)
        x = Permute((2, 1))(x)
        if x_init is not None:
            x = Add()([x, x_init])   # add skip connection
        x_init = x
        depth-=1

    x = Reshape((model_scale, 512))(x) # train against all dlatent values
    model = Model(inputs=inp,outputs=x)
    model.compile(loss='logcosh', metrics=[], optimizer='adam') # Adam optimizer, logcosh used for loss.

    return model

def finetune_resnet(model, save_path, model_res=1024, image_size=256, batch_size=10000, test_size=1000, n_epochs=10, max_patience=5, seed=0, minibatch_size=32):
    """
    Finetunes a resnet to predict W from X
    Generate batches (X, W) of size 'batch_size', iterates 'n_epochs', and repeat while 'max_patience' is reached
    on the test set. The model is saved every time a new best test loss is reached.
    """
    assert image_size >= 224

    # Create a test set
    print('Creating test set:')
    np.random.seed(seed)
    W_test, X_test = generate_dataset(n=test_size, model_res=model_res, image_size=image_size, seed=seed, minibatch_size=minibatch_size)

    # Iterate on batches of size batch_size
    print('Generating training set:')
    patience = 0
    best_loss = np.inf
    #loss = model.evaluate(X_test, W_test)
    #print('Initial test loss : {:.5f}'.format(loss))
    while (patience <= max_patience):
        W_train = X_train = None
        W_train, X_train = generate_dataset(batch_size, model_res=model_res, image_size=image_size, seed=seed, minibatch_size=minibatch_size)
        model.fit(X_train, W_train, epochs=n_epochs, verbose=True, batch_size=minibatch_size)
        loss = model.evaluate(X_test, W_test, batch_size=minibatch_size)
        if loss < best_loss:
            print('New best test loss : {:.5f}'.format(loss))
            patience = 0
            best_loss = loss
        else:
            print('Test loss : {:.5f}'.format(loss))
            patience += 1
        if (patience > max_patience): # When done with test set, train with it and discard.
            print('Done with current test set.')
            model.fit(X_test, W_test, epochs=n_epochs, verbose=True, batch_size=minibatch_size)
        print('Saving model.')
        model.save(save_path)

parser = argparse.ArgumentParser(description='Train a ResNet to predict latent representations of images in a StyleGAN model from generated examples', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('--model_url', default='https://drive.google.com/uc?id=1MEGjdvVpUsu1jB4zrXZN7Y4kBBOzizDQ', help='Fetch a StyleGAN model to train on from this URL')
parser.add_argument('--model_res', default=1024, help='The dimension of images in the StyleGAN model', type=int)
parser.add_argument('--data_dir', default='data', help='Directory for storing the ResNet model')
parser.add_argument('--model_path', default='data/finetuned_resnet.h5', help='Save / load / create the ResNet model with this file path')
parser.add_argument('--model_depth', default=1, help='Number of TreeConnect layers to add after ResNet', type=int)
parser.add_argument('--model_size', default=0, help='Model size - 0 - small, 1 - medium, 2 - large.', type=int)
parser.add_argument('--activation', default='elu', help='Activation function to use after ResNet')
parser.add_argument('--use_fp16', default=False, help='Use 16-bit floating point', type=bool)
parser.add_argument('--image_size', default=256, help='Size of images for ResNet model', type=int)
parser.add_argument('--batch_size', default=2048, help='Batch size for training the ResNet model', type=int)
parser.add_argument('--test_size', default=512, help='Batch size for testing the ResNet model', type=int)
parser.add_argument('--max_patience', default=2, help='Number of iterations to wait while test loss does not improve', type=int)
parser.add_argument('--freeze_first', default=False, help='Start training with the pre-trained network frozen, then unfreeze', type=bool)
parser.add_argument('--epochs', default=2, help='Number of training epochs to run for each batch', type=int)
parser.add_argument('--minibatch_size', default=16, help='Size of minibatches for training and generation', type=int)
parser.add_argument('--seed', default=-1, help='Pick a random seed for reproducibility (-1 for no random seed selected)', type=int)
parser.add_argument('--loop', default=-1, help='Run this many iterations (-1 for infinite, halt with CTRL-C)', type=int)

args, other_args = parser.parse_known_args()

os.makedirs(args.data_dir, exist_ok=True)

if args.seed == -1:
    args.seed = None

if args.use_fp16:
    K.set_floatx('float16')
    K.set_epsilon(1e-4) 

model = get_resnet_model(args.model_path, model_res=args.model_res, depth=args.model_depth, size=args.model_size, activation=args.activation)

tflib.init_tf()
with dnnlib.util.open_url(args.model_url, cache_dir=config.cache_dir) as f:
    generator_network, discriminator_network, Gs_network = pickle.load(f)

def load_Gs():
    return Gs_network

K.get_session().run(tensorflow.global_variables_initializer())

if args.freeze_first:
    model.layers[1].trainable = False
    model.compile(loss='logcosh', metrics=[], optimizer='adam') # Adam optimizer, logcosh used for loss.

model.summary()

if args.freeze_first: # run a training iteration first while pretrained model is frozen, then unfreeze.
    finetune_resnet(model, args.model_path, model_res=args.model_res, image_size=args.image_size, batch_size=args.batch_size, test_size=args.test_size, max_patience=args.max_patience, n_epochs=args.epochs, seed=args.seed, minibatch_size=args.minibatch_size)
    model.layers[1].trainable = True
    model.compile(loss='logcosh', metrics=[], optimizer='adam') # Adam optimizer, logcosh used for loss.
    model.summary()

if args.loop < 0:
    while True:
        finetune_resnet(model, args.model_path, model_res=args.model_res, image_size=args.image_size, batch_size=args.batch_size, test_size=args.test_size, max_patience=args.max_patience, n_epochs=args.epochs, seed=args.seed, minibatch_size=args.minibatch_size)
else:
    count = args.loop
    while count > 0:
        finetune_resnet(model, args.model_path, model_res=args.model_res, image_size=args.image_size, batch_size=args.batch_size, test_size=args.test_size, max_patience=args.max_patience, n_epochs=args.epochs, seed=args.seed, minibatch_size=args.minibatch_size)
        count -= 1
