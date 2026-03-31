package com.tetherlink

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.os.Bundle
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.widget.Button
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
import org.json.JSONObject
import java.io.DataInputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.net.Socket

/**
 * TetherLink – Main Activity
 *
 * Discovery flow:
 *   1. Listen on UDP 8765 for server broadcast
 *   2. Show "Connect" button with server name + resolution
 *   3. User taps Connect → TCP stream starts
 */
class MainActivity : AppCompatActivity() {

    private val SERVER_PORT    = 8080
    private val DISCOVERY_PORT = 8765

    // ── Views ─────────────────────────────────────────────────────────────────
    private lateinit var surfaceView:     SurfaceView
    private lateinit var overlayFps:      TextView
    private lateinit var discoveryLayout: View
    private lateinit var statusText:      TextView
    private lateinit var progressBar:     ProgressBar
    private lateinit var serverNameText:  TextView
    private lateinit var serverInfoText:  TextView
    private lateinit var connectButton:   Button

    private val ioScope   = CoroutineScope(Dispatchers.IO)
    private var streamJob: Job? = null
    private var listenJob: Job? = null

    // Discovered server info
    private var discoveredIp:   String? = null
    private var discoveredName: String  = "TetherLink Server"
    private var discoveredRes:  String  = ""

    // FPS tracking
    private var frameCount  = 0
    private var fpsLastTime = System.currentTimeMillis()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        surfaceView     = findViewById(R.id.surfaceView)
        overlayFps      = findViewById(R.id.overlayFps)
        discoveryLayout = findViewById(R.id.discoveryLayout)
        statusText      = findViewById(R.id.statusText)
        progressBar     = findViewById(R.id.progressBar)
        serverNameText  = findViewById(R.id.serverNameText)
        serverInfoText  = findViewById(R.id.serverInfoText)
        connectButton   = findViewById(R.id.connectButton)

        connectButton.setOnClickListener {
            val ip = discoveredIp ?: return@setOnClickListener
            startStreaming(ip)
        }

        startDiscoveryListener()
    }

    // ── UDP Discovery ─────────────────────────────────────────────────────────

    private fun startDiscoveryListener() {
        listenJob = ioScope.launch {
            setStatus("Searching for TetherLink server…")
            showProgress(true)

            try {
                val socket = DatagramSocket(DISCOVERY_PORT)
                socket.broadcast = true
                val buf    = ByteArray(1024)
                val packet = DatagramPacket(buf, buf.size)

                while (listenJob?.isActive == true && streamJob?.isActive != true) {
                    socket.receive(packet)
                    val json = JSONObject(
                        String(packet.data, 0, packet.length, Charsets.UTF_8)
                    )
                    if (json.optString("app") != "TetherLink") continue

                    val ip   = packet.address.hostAddress ?: continue
                    val name = json.optString("name", "TetherLink Server")
                    val res  = json.optString("resolution", "")

                    // Only update UI if server changed
                    if (ip != discoveredIp) {
                        discoveredIp   = ip
                        discoveredName = name
                        discoveredRes  = res

                        withContext(Dispatchers.Main) {
                            showProgress(false)
                            serverNameText.text = "💻  $name"
                            serverInfoText.text = buildString {
                                append(ip)
                                if (res.isNotEmpty()) append("  •  $res")
                            }
                            connectButton.visibility = View.VISIBLE
                            statusText.text = "Server found — tap to connect"
                        }
                    }
                }
                socket.close()
            } catch (e: Exception) {
                if (listenJob?.isActive == true) {
                    setStatus("Discovery error: ${e.message}")
                }
            }
        }
    }

    // ── Streaming ─────────────────────────────────────────────────────────────

    private fun startStreaming(ip: String) {
        // Hide discovery UI, show surface
        discoveryLayout.visibility = View.GONE
        surfaceView.visibility     = View.VISIBLE
        overlayFps.visibility      = View.VISIBLE
        listenJob?.cancel()

        streamJob = ioScope.launch {
            try {
                val socket = Socket()
                socket.connect(InetSocketAddress(ip, SERVER_PORT), 5000)
                val input = DataInputStream(socket.getInputStream())

                val streamW = input.readInt()
                val streamH = input.readInt()

                showToast("Connected to $discoveredName — ${streamW}×${streamH}")

                val opts = BitmapFactory.Options().apply { inMutable = true }
                var reuseBitmap: Bitmap? = null

                while (streamJob?.isActive == true) {
                    val frameSize = input.readInt()
                    if (frameSize <= 0) continue

                    val buf = ByteArray(frameSize)
                    input.readFully(buf)

                    opts.inBitmap = reuseBitmap
                    val bitmap = try {
                        BitmapFactory.decodeByteArray(buf, 0, frameSize, opts)
                    } catch (_: IllegalArgumentException) {
                        opts.inBitmap = null
                        BitmapFactory.decodeByteArray(buf, 0, frameSize, opts)
                    } ?: continue

                    reuseBitmap = bitmap
                    drawFrame(bitmap)
                    updateFps()
                }

                socket.close()

            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    // Return to discovery screen on disconnect
                    surfaceView.visibility     = View.GONE
                    overlayFps.visibility      = View.GONE
                    discoveryLayout.visibility = View.VISIBLE
                    connectButton.visibility   = View.GONE
                    discoveredIp               = null
                }
                setStatus("Disconnected — searching again…")
                showProgress(true)
                startDiscoveryListener()
            }
        }
    }

    // ── Rendering ─────────────────────────────────────────────────────────────

    private fun drawFrame(bitmap: Bitmap) {
        val holder: SurfaceHolder = surfaceView.holder
        val canvas: Canvas = holder.lockCanvas() ?: return
        try {
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

    // ── Helpers ───────────────────────────────────────────────────────────────

    private suspend fun setStatus(msg: String) = withContext(Dispatchers.Main) {
        statusText.text = msg
    }

    private suspend fun showProgress(show: Boolean) = withContext(Dispatchers.Main) {
        progressBar.visibility = if (show) View.VISIBLE else View.GONE
    }

    private suspend fun showToast(msg: String) = withContext(Dispatchers.Main) {
        Toast.makeText(this@MainActivity, msg, Toast.LENGTH_SHORT).show()
    }

    override fun onDestroy() {
        super.onDestroy()
        streamJob?.cancel()
        listenJob?.cancel()
        ioScope.cancel()
    }
}