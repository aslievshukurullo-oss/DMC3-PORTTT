package com.game.engine

import android.opengl.Matrix

class GameCamera {
    val projectionMatrix = FloatArray(16)
    val viewMatrix = FloatArray(16)

    private var camX = 0f
    private var camY = 5f
    private var camZ = 10f

    fun update(targetX: Float, targetY: Float, targetZ: Float, deltaTime: Float) {
        val targetCamX = targetX
        val targetCamY = targetY + 4f
        val targetCamZ = targetZ + 8f

        camX += (targetCamX - camX) * 5f * deltaTime
        camY += (targetCamY - camY) * 5f * deltaTime
        camZ += (targetCamZ - camZ) * 5f * deltaTime
    }

    fun computeViewMatrix() {
        Matrix.setLookAtM(
            viewMatrix, 0,
            camX, camY, camZ,
            camX, camY - 2f, camZ - 6f,
            0f, 1f, 0f
        )
    }
}
