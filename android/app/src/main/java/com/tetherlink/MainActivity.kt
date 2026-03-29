package com.tetherlink

import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.View
import android.widget.ImageView
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
 * TetherLink – Main Activity
 *
 * Scans known USB-tethering subnets to find the PC server automatically,
 * then streams JPEG frames fullscreen in landscape orientation.
 *
 * Subnets scanned (in order):
 *   10.121.104.x  — Samsung / USB-C tethering (your current setup)
 *   192.168.42.x  — standard Android tethering
 *
 * Protocol: [4-byte big-endian size][JPEG data] repeated per frame.
 */
class MainActivity : AppCompatActivity() {

    private val SERVER_PORT = 8080
    private val CONNECT_TIMEOUT_MS = 300

    // Subnet + candidate host octets to probe. .1 is the tablet, then DHCP range.
    private val SCAN_TARGETS: List<String> by lazy {
        val hosts = listOf(1) + (40..60).toList() + (100..200).toList()
        val subnets = listOf("10.121.104", "192.168.42")
        subnets.flatMap { subnet -> hosts.map { host -> "$subnet.$host" } }
    }

    private lateinit var frameView: ImageView
    private lateinit var statusText: TextView
    private lateinit var progressBar: ProgressBar

    private val ioScope = CoroutineScope(Dispatchers.IO)
    private var streamJob: Job? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        frameView   = findViewById(R.id.frameView)
        statusText  = findViewById(R.id.statusText)
        progressBar = findViewById(R.id.progressBar)

        startDiscovery()
    }

    // ── Discovery ─────────────────────────────────────────────────────────────

    private fun startDiscovery() {
        streamJob = ioScope.launch {
            setStatus("Scanning for TetherLink server...")

            val serverIp = findServer()

            if (serverIp == null) {
                setStatus("No server found.\n\nMake sure:\n• Python server is running on your PC\n• USB Tethering is enabled on this tablet")
                withContext(Dispatchers.Main) {
                    progressBar.visibility = View.GONE
                }
                return@launch
            }

            setStatus("Found server at $serverIp — connecting...")
            streamFrom(serverIp)
        }
    }

    /**
     * Probes each candidate IP sequentially.
     * Returns the first one that accepts a TCP connection on SERVER_PORT.
     */
    private fun findServer(): String? {
        for (ip in SCAN_TARGETS) {
            if (streamJob?.isActive != true) return null
            if (isPortOpen(ip)) return ip
        }
        return null
    }

    private fun isPortOpen(ip: String): Boolean {
        return try {
            val socket = Socket()
            socket.connect(InetSocketAddress(ip, SERVER_PORT), CONNECT_TIMEOUT_MS)
            socket.close()
            true
        } catch (e: Exception) {
            false
        }
    }

    // ── Streaming ─────────────────────────────────────────────────────────────

    private suspend fun streamFrom(ip: String) {
        try {
            val socket = Socket()
            socket.connect(InetSocketAddress(ip, SERVER_PORT), 3000)
            val input = DataInputStream(socket.getInputStream())

            withContext(Dispatchers.Main) {
                findViewById<View>(R.id.loadingOverlay).visibility = View.GONE
            }

            showToast("Connected to $ip:$SERVER_PORT")

            while (streamJob?.isActive == true) {
                val frameSize = input.readInt()
                if (frameSize <= 0) continue

                val buffer = ByteArray(frameSize)
                input.readFully(buffer)

                val bitmap = BitmapFactory.decodeByteArray(buffer, 0, frameSize)
                if (bitmap != null) {
                    withContext(Dispatchers.Main) {
                        frameView.setImageBitmap(bitmap)
                    }
                }
            }

            socket.close()

        } catch (e: Exception) {
            setStatus("Disconnected: ${e.message}\n\nRestart the app to reconnect.")
            withContext(Dispatchers.Main) {
                progressBar.visibility = View.GONE
                findViewById<View>(R.id.loadingOverlay).visibility = View.VISIBLE
            }
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private suspend fun setStatus(message: String) {
        withContext(Dispatchers.Main) {
            statusText.text = message
        }
    }

    private suspend fun showToast(message: String) {
        withContext(Dispatchers.Main) {
            Toast.makeText(this@MainActivity, message, Toast.LENGTH_SHORT).show()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        streamJob?.cancel()
        ioScope.cancel()
    }
}