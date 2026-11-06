package com.game.engine

import android.annotation.SuppressLint
import android.app.Activity
import android.opengl.GLSurfaceView
import android.os.Bundle
import android.view.MotionEvent
import android.widget.FrameLayout
import android.widget.TextView
import android.graphics.Color
import android.view.Gravity

class MainActivity : Activity() {
    private lateinit var glSurfaceView: GLSurfaceView
    private lateinit var gameInput: GameInput

    @SuppressLint("ClickableViewAccessibility")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        gameInput = GameInput()
        glSurfaceView = GLSurfaceView(this).apply {
            setEGLContextClientVersion(3)
            setRenderer(CoreRenderer(context, gameInput))
        }

        val rootLayout = FrameLayout(this).apply {
            addView(glSurfaceView)
        }

        val hudLayout = FrameLayout(this).apply {
            addView(TextView(context).apply {
                text = "[ Джойстик (Слева) ]"
                setTextColor(Color.argb(150, 255, 255, 255))
                textSize = 14f
                gravity = Gravity.BOTTOM or Gravity.START
                setPadding(50, 50, 50, 50)
            })
            addView(TextView(context).apply {
                text = "[ ПРЫЖОК ]\n\n[ АТАКА ]"
                setTextColor(Color.argb(180, 255, 100, 100))
                textSize = 16f
                gravity = Gravity.BOTTOM or Gravity.END
                setPadding(50, 50, 50, 50)
            })
        }
        rootLayout.addView(hudLayout)

        rootLayout.setOnTouchListener { _, event ->
            val action = event.actionMasked
            val pointerIndex = event.actionIndex
            val pointerId = event.getPointerId(pointerIndex)
            val x = event.getX(pointerIndex)
            val y = event.getY(pointerIndex)
            val width = rootLayout.width.toFloat()
            val height = rootLayout.height.toFloat()

            when (action) {
                MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN -> {
                    gameInput.onTouchDown(pointerId, x, y, width.toInt(), height.toInt())
                }
                MotionEvent.ACTION_MOVE -> {
                    for (i in 0 until event.pointerCount) {
                        gameInput.onTouchMove(event.getPointerId(i), event.getX(i), event.getY(i))
                    }
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP, MotionEvent.ACTION_CANCEL -> {
                    gameInput.onTouchUp(pointerId)
                }
            }
            true
        }

        setContentView(rootLayout)
    }

    override fun onResume() {
        super.onResume()
        glSurfaceView.onResume()
    }

    override fun onPause() {
        super.onPause()
        glSurfaceView.onPause()
    }
}
