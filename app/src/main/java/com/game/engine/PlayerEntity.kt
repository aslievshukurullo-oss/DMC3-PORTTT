package com.game.engine

import android.opengl.Matrix

class PlayerEntity {
    var posX = 0f
    var posY = 0f
    var posZ = 0f

    val modelMatrix = FloatArray(16)
    val mvpMatrix = FloatArray(16)

    fun handleInput(stickX: Float, stickY: Float, camera: GameCamera, isJumpPressed: Boolean, deltaTime: Float) {
        val speed = 5f
        posX += stickX * speed * deltaTime
        posZ += stickY * speed * deltaTime
    }

    fun updateMVP(viewMatrix: FloatArray, projectionMatrix: FloatArray) {
        Matrix.setIdentityM(modelMatrix, 0)
        Matrix.translateM(modelMatrix, 0, posX, posY, posZ)

        val tempMatrix = FloatArray(16)
        Matrix.multiplyMM(tempMatrix, 0, viewMatrix, 0, modelMatrix, 0)
        Matrix.multiplyMM(mvpMatrix, 0, projectionMatrix, 0, tempMatrix, 0)
    }
}
