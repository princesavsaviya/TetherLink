package com.tetherlink

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaCodec
import android.media.MediaFormat
import android.util.Log
import android.view.Surface

/**
 * TetherLink Stream Decoder (v0.9.0)
 * H.264: MediaCodec hardware decoder → renders to Surface
 * JPEG:  BitmapFactory → onBitmap callback
 *
 * Thread safety: decodeFrame() and release() are synchronized.
 * Warm-up: first 60 frames don't count toward failure threshold
 *          because the decoder needs SPS/PPS before it can produce output.
 */
class StreamDecoder(
    private val surface: Surface,
    val codec: Int,
    private val width: Int,
    private val height: Int,
    private val onBitmap: ((Bitmap) -> Unit)? = null
) {
    companion object {
        const val CODEC_H264 = 1
        const val CODEC_JPEG = 2
        private const val TAG = "StreamDecoder"
        private const val WARMUP_FRAMES   = 60   // ignore errors during warmup
        private const val MAX_FAIL_COUNT  = 30   // failures after warmup before giving up
    }

    @Volatile private var released = false
    private val codecLock = Any()
    private var mediaCodec: MediaCodec? = null

    private var framesSeen  = 0
    private var failCount   = 0

    // Only report not working after sustained failures post-warmup
    val isWorking: Boolean
        get() = !released &&
                (framesSeen < WARMUP_FRAMES || failCount < MAX_FAIL_COUNT)

    init {
        if (codec == CODEC_H264) setupH264Decoder()
    }

    private fun setupH264Decoder() {
        if (!surface.isValid) {
            Log.e(TAG, "Surface not valid — falling back to JPEG")
            return
        }
        try {
            val format = MediaFormat.createVideoFormat(
                MediaFormat.MIMETYPE_VIDEO_AVC, width, height
            ).apply {
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 512 * 1024)
                setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
            }
            synchronized(codecLock) {
                val mc = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
                mc.configure(format, surface, null, 0)
                mc.start()
                mediaCodec = mc
            }
            Log.i(TAG, "H.264 decoder started ${width}x${height}")
        } catch (e: Exception) {
            Log.e(TAG, "H.264 setup failed: ${e.message}")
        }
    }

    fun decodeFrame(data: ByteArray) {
        if (released) return
        framesSeen++
        when (codec) {
            CODEC_H264 -> decodeH264(data)
            CODEC_JPEG -> decodeJpeg(data)
        }
    }

    private fun decodeH264(nalUnit: ByteArray) {
        synchronized(codecLock) {
            if (released) return
            val mc = mediaCodec ?: return

            try {
                // Feed input
                val inputIdx = mc.dequeueInputBuffer(0L)
                if (inputIdx >= 0) {
                    val buf = mc.getInputBuffer(inputIdx) ?: return
                    buf.clear()
                    if (nalUnit.size <= buf.capacity()) {
                        buf.put(nalUnit)
                        mc.queueInputBuffer(
                            inputIdx, 0, nalUnit.size,
                            System.nanoTime() / 1000, 0
                        )
                    }
                }

                // Drain all available output frames
                val info = MediaCodec.BufferInfo()
                var outIdx = mc.dequeueOutputBuffer(info, 0L)
                while (outIdx >= 0) {
                    mc.releaseOutputBuffer(outIdx, true) // render=true → to Surface
                    outIdx = mc.dequeueOutputBuffer(info, 0L)
                }

                // Reset fail count on success
                if (framesSeen > WARMUP_FRAMES) failCount = 0

            } catch (e: Exception) {
                if (framesSeen > WARMUP_FRAMES) {
                    failCount++
                    if (failCount % 10 == 0) {
                        Log.w(TAG, "H.264 errors: $failCount — ${e.message}")
                    }
                }
                // else: warmup errors are expected and ignored
            }
        }
    }

    private fun decodeJpeg(data: ByteArray) {
        BitmapFactory.decodeByteArray(data, 0, data.size)?.let { onBitmap?.invoke(it) }
    }

    fun release() {
        released = true
        synchronized(codecLock) {
            try {
                mediaCodec?.stop()
                mediaCodec?.release()
            } catch (e: Exception) {
                Log.w(TAG, "Release error: ${e.message}")
            }
            mediaCodec = null
        }
        Log.i(TAG, "Decoder released")
    }
}