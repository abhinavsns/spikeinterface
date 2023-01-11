import warnings

import numpy as np

from spikeinterface.core.channelslice import ChannelSliceRecording
from spikeinterface.core.core_tools import define_function_from_class

from .basepreprocessor import BasePreprocessor

from ..core import get_random_data_chunks

import scipy.stats

class RemoveBadChannelsRecording(BasePreprocessor, ChannelSliceRecording):
    """
    Remove bad channels from the recording extractor given a thershold
    on standard deviation.

    Parameters
    ----------
    recording: RecordingExtractor
        The recording extractor object
    bad_threshold: float
        If automatic is used, the threshold for the standard deviation over which channels are removed
    **random_chunk_kwargs

    Returns
    -------
    remove_bad_channels_recording: RemoveBadChannelsRecording
        The recording extractor without bad channels
    """
    name = 'remove_bad_channels'

    def __init__(self, recording, bad_threshold=5, **random_chunk_kwargs):
        random_data = get_random_data_chunks(recording, **random_chunk_kwargs)

        stds = np.std(random_data, axis=0)
        thresh = bad_threshold * np.median(stds)
        keep_inds, = np.nonzero(stds < thresh)

        parents_chan_ids = recording.get_channel_ids()
        channel_ids = parents_chan_ids[keep_inds]
        self._parent_channel_indices = recording.ids_to_indices(channel_ids)

        BasePreprocessor.__init__(self, recording)
        ChannelSliceRecording.__init__(self, recording, channel_ids=channel_ids)

        self._kwargs = dict(recording=recording.to_dict(), bad_threshold=bad_threshold)
        self._kwargs.update(random_chunk_kwargs)


# function for API
remove_bad_channels = define_function_from_class(source_class=RemoveBadChannelsRecording, name="remove_bad_channels")

# ------------------------------------------------------------------------------------------
# SpikeInterface Detect Bad Channels
# ------------------------------------------------------------------------------------------

def detect_bad_channels(recording,
                        psd_hf_threshold=None,
                        similarity_threshold=(-0.5, 1),
                        random_chunk_kwargs=None):
    """
    Perform bad channel detection using the detect_bad_channels
    algorithm of the IBL.

    Detects bad channels of three types:
        Dead channels are those with low similarity to the surrounding channels
            (n=11 median)
        Noise channels are those with power at >80% Nyquist above the
            psd_hf_threshold (default 0.02 uV^2 / Hz)
        Out of brain channels are contigious regions of channels
            dissimilar to the median of all channels at the top end
            of the probe (i.e. large channel number)

    Parameters
    ----------

    psd_hf_threshold: an absolute threshold (uV^2/Hz) used as a cutoff for
                      noise channels. Channels with average power at >80%
                      Nyquist larger than this threshold will be labelled
                      as noise.

    similarity_threshold: absolute threshold for channel similarity,
                          in the form for a tuple (threshold1, threshold2).
                          In dead channel detection, the median filtered
                          cross-correlation (N=11) is subtracted from the
                          cross-correlation. When a channel is not similar
                          from the surrounding channels, there are large
                          residuals (negative when a channel has a
                          negative cross-correlation with all other channels,
                          positive when it has a positive cross-correlation.
                          This is termed the 'similarity' measure.

                          threshold1 (default -0.5) tags any channel with
                          a similarity measure < -0.05 (i.e., there is a
                          large negative-direction different between the
                          xcor of this channel with all other channels,
                          and the nearest 11 channels.

                          threshold2 is used in noise detection, channels
                          with similarity measure > threshold2 are tagged
                          as noise (i.e. the channel is much more strongly
                          correlated with all other channels than the surrounding
                          11 channels).

    random_chunk_kwargs: a dictionary with keys passed to get_random_data_chunks()

    see for details:
        International Brain Laboratory et al. (2022). Spike sorting pipeline for the
        International Brain Laboratory. https://www.internationalbrainlab.com/repro-ephys
    """
    if psd_hf_threshold is None:
        psd_hf_threshold = 1.4 if fs < 5000 else 0.02

    # Get random subset of data to estimate from
    random_chunk_kwargs, scale_for_testing = handle_random_chunk_kwargs(recording,
                                                                        random_chunk_kwargs)
    random_data = get_random_data_chunks(recording, **random_chunk_kwargs)

    # Create empty channel labels and fill with bad-channel detection
    # estimate for each chunk
    channel_labels = np.zeros((recording.get_num_channels(),
                               recording.get_num_segments() * random_chunk_kwargs["num_chunks_per_segment"]))

    for i, random_chunk in enumerate(random_data):

        channel_labels[:, i], __ = detect_bad_channels_ibl(random_chunk,
                                                           recording.get_sampling_frequency(),
                                                           psd_hf_threshold,
                                                           similarity_threshold,
                                                           scale_for_testing)

    # Take the mode of the chunk estimates as final result. Convert to
    # binary good / bad channel output.
    channel_flags, __ = scipy.stats.mode(channel_labels, axis=1, keepdims=False)

    bad_inds, = np.where(channel_flags != 0)
    bad_channel_ids = recording.get_channel_ids()[bad_inds]

    if bad_channel_ids.size > recording.get_num_channels() * 0.333:
        warnings.warn("Over 1/3 of channels are detected as bad. In the precense of a high"
                      "number of dead / noisy channels, bad channel detection may fail "
                      "(erroneously label good channels as dead).")

    return bad_inds, bad_channel_ids, channel_flags


def handle_random_chunk_kwargs(recording, user_random_chunk_kwargs):
    """
    Here we want to overwrite the default random_chunk_kwargs,
    but allow the user to overwrite these with their own options.
    Make default random chunk kwargs and overwrite with any user-specified.

    The default chunk size of 0.3 s, 10 chunks is taken
    from IBL implementation.

    To add scaling in detect_bad_channels_ibl() to match IBL
    original function for testing against, a hidden flag on
    the kwargs is used, "scale_for_testing".
    """
    if ("concatenated" in user_random_chunk_kwargs and
        user_random_chunk_kwargs["concatenated"]):
        raise AttributeError("Custom random_chunk_kwargs cannot included data concatenation")

    chunk_size = int(0.3 * recording.get_sampling_frequency())
    random_chunk_kwargs = {"return_scaled": True,
                           "num_chunks_per_segment": 10,
                           "chunk_size": chunk_size,
                           "concatenated": False,
                           "seed": 0}

    if user_random_chunk_kwargs is not None:
        random_chunk_kwargs.update(user_random_chunk_kwargs)

    scale_for_testing = handle_test_case(random_chunk_kwargs)

    return random_chunk_kwargs, scale_for_testing

def handle_test_case(scale_for_testing):
    """
    see test_remove_bad_channels() for logic
    """
    if "scale_for_testing" in scale_for_testing:
        scale_for_testing.pop("scale_for_testing")
        scale_for_testing = True
    else:
        scale_for_testing = False

    return scale_for_testing

# ----------------------------------------------------------------------------------------------
# IBL Detect Bad Channels
# ----------------------------------------------------------------------------------------------

def detect_bad_channels_ibl(raw, fs, psd_hf_threshold, similarity_threshold=(-0.5, 1), scale_for_testing=False):
    """
    Bad channels detection for Neuropixel probes
    Labels channels
     0: all clear
     1: dead low coherence / amplitude
     2: noisy
     3: outside of the brain
    :param raw: [nc, ns]
    :param fs: sampling frequency
    :param similarity_threshold:
    :param psd_hf_threshold:
    :return: labels (numpy vector [nc]), xfeats: dictionary of features [nc]
    """
    __, nc = raw.shape
    raw = raw - np.mean(raw, axis=0)[np.newaxis, :]
    xcor = channel_similarity(raw)

    xcor_new = channel_simiarlity_new(raw)
    assert np.allclose(xcor, xcor_new, atol=0.0001, rtol=0)

    scale = 1e6 if scale_for_testing else 1
    fscale, psd = scipy.signal.welch(raw * scale, fs=fs, axis=0)
    sos_hp = scipy.signal.butter(**{'N': 3, 'Wn': 300 / fs * 2, 'btype': 'highpass'}, output='sos')  # dupl
    hf = scipy.signal.sosfiltfilt(sos_hp, raw, axis=0)
    xcorf = channel_similarity(hf)

    xfeats = ({
        'ind': np.arange(nc),
        'rms_raw': rms(raw, axis=0),  # very similar to the rms after butterworth filter
        'xcor_hf': detrend(xcor, 11),
        'xcor_lf': xcorf - detrend(xcorf, 11) - 1,
        'psd_hf': np.mean(psd[fscale > (fs / 2 * 0.8), :], axis=0),  # 80% nyquists
    })

    # make recommendation
    ichannels = np.zeros(nc)
    idead = np.where(similarity_threshold[0] > xfeats['xcor_hf'])[0]
    inoisy = np.where(np.logical_or(xfeats['psd_hf'] > psd_hf_threshold, xfeats['xcor_hf'] > similarity_threshold[1]))[0]

    # the channels outside of the brains are the contiguous channels below the threshold on the trend coherency
    ioutside = np.where(xfeats['xcor_lf'] < -0.75)[0]
    if ioutside.size > 0 and ioutside[-1] == (nc - 1):
        a = np.cumsum(np.r_[0, np.diff(ioutside) - 1])
        ioutside = ioutside[a == np.max(a)]
        ichannels[ioutside] = 3

    ichannels[idead] = 1
    ichannels[inoisy] = 2

    return ichannels, xfeats

# ----------------------------------------------------------------------------------------------
# IBL Helpers
# ----------------------------------------------------------------------------------------------

def rms(x, axis=-1):
    """
    Root mean square of array along axis
    :param x: array on which to compute RMS
    :param axis: (optional, -1)
    :return: numpy array
    """
    return np.sqrt(np.mean(x ** 2, axis=axis))

def detrend(x, nmed):
    """
    Subtract the trend from a vector
    The trend is a median filtered version of the said vector with tapering
    :param x: input vector
    :param nmed: number of points of the median filter
    :return: np.array
    """
    ntap = int(np.ceil(nmed / 2))
    xf = np.r_[np.zeros(ntap) + x[0], x, np.zeros(ntap) + x[-1]]

    xf = scipy.signal.medfilt(xf, nmed)[ntap:-ntap]
    return x - xf

def channel_simiarlity_new(raw):
    ref = np.median(raw, axis=1)
    channel_corr_with_median = np.sum(raw * ref[:, np.newaxis], axis=0) / np.sum(ref**2)
    return channel_corr_with_median


def channel_similarity(raw, nmed=0):
    """"""
    ref = np.median(raw, axis=1)
    xcor = nxcor(raw, ref)
    alt_comp = np.sum(raw * ref[:, np.newaxis], axis=0) / np.sum(ref ** 2)
    assert np.allclose(xcor, alt_comp, rtol=0, atol=0.01)

    return xcor

def fxcor(x, y):
    n = x.shape[0]
    return scipy.fft.irfft(scipy.fft.rfft(x, axis=0) * np.conj(scipy.fft.rfft(y, axis=0)), axis=0, n=n)

def nxcor(x, ref):
    ref = ref - np.mean(ref)
    apeak = fxcor(ref, ref)[0] # get the sum of squares in the reference i.e. unnormalised cross correlation with no shift
    x = x - np.mean(x, axis=0)
    np.sum(x[:, 0] * ref) / np.sum(ref**2)
    return fxcor(x, ref[:, np.newaxis])[0, :] / apeak

