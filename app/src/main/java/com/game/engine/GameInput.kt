package com.game.engine

class GameInput {
    var stickX: Float = 0f
    var stickY: Float = 0f
    var isAttackPressed: Boolean = false
    var isJumpPressed: Boolean = false

    private var joystickTouchId = -1
    private var centerX = 0f
    private var centerY = 0f

    fun onTouchDown(pointerId: Int, x: Float, y: Float, screenWidth: Int, screenHeight: Int) {
        if (x < screenWidth / 2f && joystickTouchId == -1) {
            joystickTouchId = pointerId
            centerX = x
            centerY = y
        } else if (x >= screenWidth / 2f) {
            if (y < screenHeight / 2f) {
                isJumpPressed = true
            } else {
                isAttackPressed = true
            }
        }
    }

    fun onTouchMove(pointerId: Int, x: Float, y: Float) {
        if (pointerId == joystickTouchId) {
            val dx = x - centerX
            val dy = y - centerY
            val maxRadius = 150f
            
            stickX = (dx / maxRadius).coerceIn(-1f, 1f)
            stickY = (dy / maxRadius).coerceIn(-1f, 1f)
        }
    }

    fun onTouchUp(pointerId: Int) {
        if (pointerId == joystickTouchId) {
            joystickTouchId = -1
            stickX = 0f
            stickY = 0f
        }
    }

    fun resetTriggers() {
        isAttackPressed = false
        isJumpPressed = false
    }
}
