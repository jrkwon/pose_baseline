
"""Predicting 3d poses from 2d joints"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import time
import h5py
import copy

import matplotlib.pyplot as plt
import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
import procrustes

import viz
import cameras
import data_utils
import linear_model

tf.app.flags.DEFINE_float("learning_rate", 1e-3, "Learning rate")
tf.app.flags.DEFINE_float("dropout", 0.5, "Dropout keep probability. 1 means no dropout")
tf.app.flags.DEFINE_integer("batch_size", 64, "Batch size to use during training")
tf.app.flags.DEFINE_integer("epochs", 1, "How many epochs we should train for")
tf.app.flags.DEFINE_boolean("camera_frame", False, "Convert 3d poses to camera coordinates")
tf.app.flags.DEFINE_boolean("max_norm", True, "Apply maxnorm constraint to the weights")
tf.app.flags.DEFINE_boolean("batch_norm", False, "Use batch_normalization")

# Data loading
tf.app.flags.DEFINE_boolean("predict_14", False, "predict 14 joints")
tf.app.flags.DEFINE_boolean("use_sh", True, "Use 2d pose predictions from StackedHourglass")
tf.app.flags.DEFINE_string("action","All", "The action to train on. 'All' means all the actions")

# Architecture
tf.app.flags.DEFINE_integer("linear_size", 1024, "Size of each model layer.")
tf.app.flags.DEFINE_integer("num_layers", 2, "Number of layers in the model.")
tf.app.flags.DEFINE_boolean("residual", True, "Whether to add a residual connection every 2 layers")

# Evaluation
tf.app.flags.DEFINE_boolean("procrustes", False, "Apply procrustes analysis at test time")
tf.app.flags.DEFINE_boolean("evaluateActionWise",True, "The dataset to use either h36m or heva")

# Directories
tf.app.flags.DEFINE_string("cameras_path","data/h36m/cameras.h5","Directory to load camera parameters")
tf.app.flags.DEFINE_string("data_dir",   "data/h36m/", "Data directory")
tf.app.flags.DEFINE_string("train_dir", "experiments", "Training directory.")

# Train or load
tf.app.flags.DEFINE_boolean("sample", False, "Set to True for sampling.")
tf.app.flags.DEFINE_boolean("use_cpu", False, "Whether to use the CPU")
tf.app.flags.DEFINE_integer("load", 24371, "Try to load a previous checkpoint.")

# Misc
tf.app.flags.DEFINE_boolean("use_fp16", False, "Train using fp16 instead of fp32.")

FLAGS = tf.app.flags.FLAGS

train_dir = os.path.join( FLAGS.train_dir,
  FLAGS.action,
  'dropout_{0}'.format(FLAGS.dropout),
  'epochs_{0}'.format(FLAGS.epochs) if FLAGS.epochs > 0 else '',
  'lr_{0}'.format(FLAGS.learning_rate),
  'residual' if FLAGS.residual else 'not_residual',
  'depth_{0}'.format(FLAGS.num_layers),
  'linear_size{0}'.format(FLAGS.linear_size),
  'batch_size_{0}'.format(FLAGS.batch_size),
  'procrustes' if FLAGS.procrustes else 'no_procrustes',
  'maxnorm' if FLAGS.max_norm else 'no_maxnorm',
  'batch_normalization' if FLAGS.batch_norm else 'no_batch_normalization',
  'use_stacked_hourglass' if FLAGS.use_sh else 'not_stacked_hourglass',
  'predict_14' if FLAGS.predict_14 else 'predict_17')

print( train_dir )
summaries_dir = os.path.join( train_dir, "log" ) # Directory for TB summaries

# To avoid race conditions: https://github.com/tensorflow/tensorflow/issues/7448
os.system('mkdir -p {}'.format(summaries_dir))

def create_model( session, actions, batch_size ):
  """
  Create model and initialize it or load its parameters in a session

  Args
    session: tensorflow session
    actions: list of string. Actions to train/test on
    batch_size: integer. Number of examples in each batch
  Returns
    model: The created (or loaded) model
  Raises
    ValueError if asked to load a model, but the checkpoint specified by
    FLAGS.load cannot be found.
  """

  model = linear_model.LinearModel(
      FLAGS.linear_size,
      FLAGS.num_layers,
      FLAGS.residual,
      FLAGS.batch_norm,
      FLAGS.max_norm,
      batch_size,
      FLAGS.learning_rate,
      summaries_dir,
      FLAGS.predict_14,
      dtype=tf.float16 if FLAGS.use_fp16 else tf.float32)

  if FLAGS.load <= 0:
    # Create a new model from scratch
    print("Creating model with fresh parameters.")
    session.run( tf.global_variables_initializer() )
    return model

  # Load a previously saved model
  ckpt = tf.train.get_checkpoint_state( train_dir, latest_filename="checkpoint")
  print( "train_dir", train_dir )

  if ckpt and ckpt.model_checkpoint_path:
    # Check if the specific checkpoint exists
    if FLAGS.load > 0:
      if os.path.isfile(os.path.join(train_dir,"checkpoint-{0}.index".format(FLAGS.load))):
        ckpt_name = os.path.join( os.path.join(train_dir,"checkpoint-{0}".format(FLAGS.load)) )
      else:
        raise ValueError("Asked to load checkpoint {0}, but it does not seem to exist".format(FLAGS.load))
    else:
      ckpt_name = os.path.basename( ckpt.model_checkpoint_path )

    print("Loading model {0}".format( ckpt_name ))
    model.saver.restore( session, ckpt.model_checkpoint_path )
    return model
  else:
    print("Could not find checkpoint. Aborting.")
    raise( ValueError, "Checkpoint {0} does not seem to exist".format( ckpt.model_checkpoint_path ) )

  return model





def sample():
  """Get samples from a model and visualize them"""
  
  actions = [""]

  device_count = {"GPU": 0} if FLAGS.use_cpu else {"GPU": 1}
  with tf.Session(config=tf.ConfigProto( device_count = device_count )) as sess:
    # === Create the model ===
    print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.linear_size))
    batch_size = 128
    model = create_model(sess, actions, batch_size)
    print("Model loaded")

    for key2d in test_set_2d.keys():

      (subj, b, fname) = key2d
      print( "Subject: {}, action: {}, fname: {}".format(subj, b, fname) )

      # keys should be the same if 3d is in camera coordinates
      key3d = key2d if FLAGS.camera_frame else (subj, b, '{0}.h5'.format(fname.split('.')[0]))
      key3d = (subj, b, fname[:-3]) if (fname.endswith('-sh')) and FLAGS.camera_frame else key3d

      enc_in  = test_set_2d[ key2d ]
      n2d, _ = enc_in.shape
      dec_out = test_set_3d[ key3d ]
      n3d, _ = dec_out.shape
      assert n2d == n3d

      # Split into about-same-size batches
      enc_in   = np.array_split( enc_in,  n2d // batch_size )
      dec_out  = np.array_split( dec_out, n3d // batch_size )
      all_poses_3d = []

      for bidx in range( len(enc_in) ):

        # Dropout probability 0 (keep probability 1) for sampling
        dp = 1.0
        _, _, poses3d = model.step(sess, enc_in[bidx], dec_out[bidx], dp, isTraining=False)

        # denormalize
        enc_in[bidx]  = data_utils.unNormalizeData(  enc_in[bidx], data_mean_2d, data_std_2d, dim_to_ignore_2d )
        dec_out[bidx] = data_utils.unNormalizeData( dec_out[bidx], data_mean_3d, data_std_3d, dim_to_ignore_3d )
        poses3d = data_utils.unNormalizeData( poses3d, data_mean_3d, data_std_3d, dim_to_ignore_3d )
        all_poses_3d.append( poses3d )

      # Put all the poses together
      enc_in, dec_out, poses3d = map( np.vstack, [enc_in, dec_out, all_poses_3d] )

      # Convert back to world coordinates
      if FLAGS.camera_frame:
        N_CAMERAS = 4
        N_JOINTS_H36M = 32

        # Add global position back
        dec_out = dec_out + np.tile( test_root_positions[ key3d ], [1,N_JOINTS_H36M] )

        # Load the appropriate camera
        subj, _, sname = key3d

        cname = sname.split('.')[1] # <-- camera name
        scams = {(subj,c+1): rcams[(subj,c+1)] for c in range(N_CAMERAS)} # cams of this subject
        scam_idx = [scams[(subj,c+1)][-1] for c in range(N_CAMERAS)].index( cname ) # index of camera used
        the_cam  = scams[(subj, scam_idx+1)] # <-- the camera used
        R, T, f, c, k, p, name = the_cam
        assert name == cname

        def cam2world_centered(data_3d_camframe):
          data_3d_worldframe = cameras.camera_to_world_frame(data_3d_camframe.reshape((-1, 3)), R, T)
          data_3d_worldframe = data_3d_worldframe.reshape((-1, N_JOINTS_H36M*3))
          # subtract root translation
          return data_3d_worldframe - np.tile( data_3d_worldframe[:,:3], (1,N_JOINTS_H36M) )

        # Apply inverse rotation and translation
        dec_out = cam2world_centered(dec_out)
        poses3d = cam2world_centered(poses3d)

  # Grab a random batch to visualize
  enc_in, dec_out, poses3d = map( np.vstack, [enc_in, dec_out, poses3d] )
  idx = np.random.permutation( enc_in.shape[0] )
  enc_in, dec_out, poses3d = enc_in[idx, :], dec_out[idx, :], poses3d[idx, :]

  # Visualize random samples
  import matplotlib.gridspec as gridspec

  # 1080p	= 1,920 x 1,080
  fig = plt.figure( figsize=(19.2, 10.8) )

  gs1 = gridspec.GridSpec(5, 9) # 5 rows, 9 columns
  gs1.update(wspace=-0.00, hspace=0.05) # set the spacing between axes.
  plt.axis('off')

  subplot_idx, exidx = 1, 1
  nsamples = 15
  for i in np.arange( nsamples ):

    # Plot 2d pose
    ax1 = plt.subplot(gs1[subplot_idx-1])
    p2d = enc_in[exidx,:]
    viz.show2Dpose( p2d, ax1 )
    ax1.invert_yaxis()

    # Plot 3d gt
    ax2 = plt.subplot(gs1[subplot_idx], projection='3d')
    p3d = dec_out[exidx,:]
    viz.show3Dpose( p3d, ax2 )

    # Plot 3d predictions
    ax3 = plt.subplot(gs1[subplot_idx+1], projection='3d')
    p3d = poses3d[exidx,:]
    viz.show3Dpose( p3d, ax3, lcolor="#9b59b6", rcolor="#2ecc71" )

    exidx = exidx + 1
    subplot_idx = subplot_idx + 3

  plt.show()

def main(_):
  sample()


if __name__ == "__main__":
  tf.app.run()
