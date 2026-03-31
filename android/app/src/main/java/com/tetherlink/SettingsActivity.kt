package com.tetherlink

import android.content.Context
import android.content.SharedPreferences
import android.os.Bundle
import android.widget.SeekBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * TetherLink Settings (v0.7.0)
 * Allows user to configure FPS target and JPEG quality.
 * Settings are persisted in SharedPreferences and read by MainActivity.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var prefs: SharedPreferences

    private lateinit var fpsSeek:     SeekBar
    private lateinit var fpsLabel:    TextView
    private lateinit var qualSeek:    SeekBar
    private lateinit var qualLabel:   TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)

        prefs      = getSharedPreferences("tetherlink", Context.MODE_PRIVATE)
        fpsSeek    = findViewById(R.id.fpsSeekBar)
        fpsLabel   = findViewById(R.id.fpsLabel)
        qualSeek   = findViewById(R.id.qualitySeekBar)
        qualLabel  = findViewById(R.id.qualityLabel)

        // FPS: 10–60 step 5
        val savedFps = prefs.getInt("target_fps", 60)
        fpsSeek.max      = 10          // (60-10)/5 = 10 steps
        fpsSeek.progress = (savedFps - 10) / 5
        fpsLabel.text    = "Target FPS: $savedFps"

        fpsSeek.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar, progress: Int, fromUser: Boolean) {
                val fps = 10 + progress * 5
                fpsLabel.text = "Target FPS: $fps"
                prefs.edit().putInt("target_fps", fps).apply()
            }
            override fun onStartTrackingTouch(sb: SeekBar) {}
            override fun onStopTrackingTouch(sb: SeekBar) {}
        })

        // Quality: 50–95 step 5
        val savedQual = prefs.getInt("jpeg_quality", 90)
        qualSeek.max      = 9          // (95-50)/5 = 9 steps
        qualSeek.progress = (savedQual - 50) / 5
        qualLabel.text    = "JPEG Quality: $savedQual  ${qualityHint(savedQual)}"

        qualSeek.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar, progress: Int, fromUser: Boolean) {
                val qual = 50 + progress * 5
                qualLabel.text = "JPEG Quality: $qual  ${qualityHint(qual)}"
                prefs.edit().putInt("jpeg_quality", qual).apply()
            }
            override fun onStartTrackingTouch(sb: SeekBar) {}
            override fun onStopTrackingTouch(sb: SeekBar) {}
        })

        supportActionBar?.apply {
            title = "Settings"
            setDisplayHomeAsUpEnabled(true)
        }
    }

    private fun qualityHint(q: Int) = when {
        q >= 85 -> "— Sharp"
        q >= 70 -> "— Balanced"
        else    -> "— Fast"
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}
