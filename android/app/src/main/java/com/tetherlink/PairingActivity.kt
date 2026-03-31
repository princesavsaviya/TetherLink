package com.tetherlink

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.util.Base64
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanIntentResult
import com.journeyapps.barcodescanner.ScanOptions

/**
 * TetherLink Pairing Activity (v0.8.0)
 * Uses ZXing embedded scanner (no external app required).
 */
class PairingActivity : AppCompatActivity() {

    private val barcodeLauncher = registerForActivityResult(ScanContract()) { result: ScanIntentResult ->
        if (result.contents != null) {
            handleQrResult(result.contents)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_pairing)

        val statusView = findViewById<TextView>(R.id.pairingStatus)
        statusView.text = if (getStoredSecret() != null)
            "✅ Already paired — scan to re-pair"
        else
            "⚠️ Not paired — scan QR code from your PC"

        findViewById<Button>(R.id.scanQrBtn).setOnClickListener {
            val options = ScanOptions().apply {
                setPrompt("Scan the TetherLink QR code shown in your PC terminal")
                setBeepEnabled(true)
                setOrientationLocked(false)
                setBarcodeImageEnabled(false)
            }
            barcodeLauncher.launch(options)
        }

        findViewById<Button>(R.id.backBtn).setOnClickListener {
            finish()
        }
    }

    private fun handleQrResult(content: String) {
        if (content.startsWith("tetherlink://pair?key=")) {
            val keyB64 = content.removePrefix("tetherlink://pair?key=")
            try {
                val secret = Base64.decode(keyB64, Base64.DEFAULT)
                storeSecret(secret)
                Toast.makeText(this, "✅ Paired successfully!", Toast.LENGTH_LONG).show()
                finish()
            } catch (e: Exception) {
                Toast.makeText(this, "Invalid QR code: ${e.message}", Toast.LENGTH_SHORT).show()
            }
        } else {
            Toast.makeText(this, "Not a TetherLink QR code", Toast.LENGTH_SHORT).show()
        }
    }

    private fun getStoredSecret(): ByteArray? {
        val prefs = getSharedPreferences("tetherlink_security", Context.MODE_PRIVATE)
        val saved = prefs.getString("paired_secret", null) ?: return null
        return Base64.decode(saved, Base64.DEFAULT)
    }

    private fun storeSecret(secret: ByteArray) {
        getSharedPreferences("tetherlink_security", Context.MODE_PRIVATE)
            .edit()
            .putString("paired_secret", Base64.encodeToString(secret, Base64.DEFAULT))
            .apply()
    }
}