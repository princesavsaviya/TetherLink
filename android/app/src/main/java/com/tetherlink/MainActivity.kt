package com.tetherlink

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.os.Bundle
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.DataInputStream
import java.net.InetSocketAddress
import java.net.Socket

/**
 * TetherLink – Main Activity (Milestone 2)
 *
 * - Reads resolution handshake from server before streaming
 * - Renders frames via SurfaceView (no GC pressure vs ImageView)
 * - Remembers last connected IP in SharedPreferences
 * - Scans subnets automatically if no saved IP or saved IP is unreachable
 */
class MainActivity : AppCompatActivity() {

    private val SERVER_PORT        = 8080
    private val CONNECT_TIMEOUT_MS = 300
    private val PREFS_NAME         = "tetherlink"
    private val PREF_LAST_IP       = "last_ip"
    private val SUBNETS = listOf("10.90.14", "10.121.104", "192.168.42")

    private lateinit var surfaceView: SurfaceView
    private lateinit var statusText: TextView
    private lateinit var progressBar: ProgressBar
    private lateinit var overlayFps: TextView

    private val ioScope  = CoroutineScope(Dispatchers.IO)
    private var streamJob: Job? = null

    // FPS tracking
    private var frameCount  = 0
    private var fpsLastTime = System.currentTimeMillis()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        surfaceView  = findViewById(R.id.surfaceView)
        statusText   = findViewById(R.id.statusText)
        progressBar  = findViewById(R.id.progressBar)
        overlayFps   = findViewById(R.id.overlayFps)

        startConnection()
    }

    // ── Connection entry point ────────────────────────────────────────────────

    private fun startConnection() {
        streamJob = ioScope.launch {
            // Try last known IP first for instant reconnect
            val savedIp = getSavedIp()
            if (savedIp != null) {
                setStatus("Reconnecting to $savedIp…")
                if (isPortOpen(savedIp)) {
                    connectAndStream(savedIp)
                    return@launch
                }
                setStatus("$savedIp unreachable — scanning…")
            }

            // Fall back to subnet scan
            val ip = scanForServer()
            if (ip == null) {
                setStatus("No server found.\n\nMake sure:\n• Python server is running\n• USB Tethering is enabled")
                withContext(Dispatchers.Main) { progressBar.visibility = View.GONE }
                return@launch
            }

            saveIp(ip)
            connectAndStream(ip)
        }
    }

    // ── Discovery ─────────────────────────────────────────────────────────────

    private suspend fun scanForServer(): String? {
        val candidates = listOf(1) + (2..30).toList() + (40..60).toList() + (100..200).toList()
        for (subnet in SUBNETS) {
            setStatus("Scanning $subnet.0/24…")
            for (host in candidates) {
                if (streamJob?.isActive != true) return null
                val ip = "$subnet.$host"
                if (isPortOpen(ip)) return ip
            }
        }
        return null
    }

    private fun isPortOpen(ip: String): Boolean {
        return try {
            val s = Socket()
            s.connect(InetSocketAddress(ip, SERVER_PORT), CONNECT_TIMEOUT_MS)
            s.close()
            true
        } catch (e: Exception) { false }
    }

    // ── Streaming ─────────────────────────────────────────────────────────────

    private suspend fun connectAndStream(ip: String) {
        try {
            val socket = Socket()
            socket.connect(InetSocketAddress(ip, SERVER_PORT), 3000)
            val input = DataInputStream(socket.getInputStream())

            // ── Read resolution handshake (8 bytes: width + height) ───────────
            val streamW = input.readInt()
            val streamH = input.readInt()

            withContext(Dispatchers.Main) {
                // Keep SurfaceView full screen — we scale the bitmap to fill
                findViewById<View>(R.id.loadingOverlay).visibility = View.GONE
                overlayFps.visibility = View.VISIBLE
            }

            showToast("Connected to $ip — ${streamW}×${streamH}")

            // Reusable bitmap options (inBitmap recycles memory between frames)
            val opts = BitmapFactory.Options().apply { inMutable = true }
            var reuseBitmap: Bitmap? = null

            while (streamJob?.isActive == true) {
                val frameSize = input.readInt()
                if (frameSize <= 0) continue

                val buf = ByteArray(frameSize)
                input.readFully(buf)

                // Decode — try to reuse previous bitmap allocation
                opts.inBitmap = reuseBitmap
                val bitmap = try {
                    BitmapFactory.decodeByteArray(buf, 0, frameSize, opts)
                } catch (e: IllegalArgumentException) {
                    // Reuse failed (size mismatch) — decode fresh
                    opts.inBitmap = null
                    BitmapFactory.decodeByteArray(buf, 0, frameSize, opts)
                } ?: continue

                reuseBitmap = bitmap
                drawFrame(bitmap)
                updateFps()
            }

            socket.close()

        } catch (e: Exception) {
            clearSavedIp()
            setStatus("Disconnected: ${e.message}\n\nRestart to reconnect.")
            withContext(Dispatchers.Main) {
                progressBar.visibility = View.GONE
                overlayFps.visibility = View.GONE
                findViewById<View>(R.id.loadingOverlay).visibility = View.VISIBLE
            }
        }
    }

    // ── Rendering ─────────────────────────────────────────────────────────────

    private fun drawFrame(bitmap: Bitmap) {
        val holder: SurfaceHolder = surfaceView.holder
        val canvas: Canvas = holder.lockCanvas() ?: return
        try {
            // Scale bitmap to fill entire surface, preserving no aspect ratio
            // (fills screen completely — matches extend display intent)
            val dst = android.graphics.RectF(
                0f, 0f,
                canvas.width.toFloat(),
                canvas.height.toFloat()
            )
            canvas.drawColor(android.graphics.Color.BLACK)
            canvas.drawBitmap(bitmap, null, dst, null)
        } finally {
            holder.unlockCanvasAndPost(canvas)
        }
    }

    private suspend fun updateFps() {
        frameCount++
        val now = System.currentTimeMillis()
        if (now - fpsLastTime >= 1000) {
            val fps = frameCount
            frameCount  = 0
            fpsLastTime = now
            withContext(Dispatchers.Main) {
                overlayFps.text = "$fps FPS"
            }
        }
    }

    // ── Persistence ───────────────────────────────────────────────────────────

    private fun getSavedIp(): String? =
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .getString(PREF_LAST_IP, null)

    private fun saveIp(ip: String) =
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit().putString(PREF_LAST_IP, ip).apply()

    private fun clearSavedIp() =
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit().remove(PREF_LAST_IP).apply()

    // ── Helpers ───────────────────────────────────────────────────────────────

    private suspend fun setStatus(msg: String) = withContext(Dispatchers.Main) {
        statusText.text = msg
    }

    private suspend fun showToast(msg: String) = withContext(Dispatchers.Main) {
        Toast.makeText(this@MainActivity, msg, Toast.LENGTH_SHORT).show()
    }

    override fun onDestroy() {
        super.onDestroy()
        streamJob?.cancel()
        ioScope.cancel()
    }
}