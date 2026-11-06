package com.game.engine

import kotlin.math.sqrt

class EnemyDummy {
    var posX = 2f
    var posY = 0f
    var posZ = -2f
    private var health = 100

    fun update(deltaTime: Float, playerX: Float, playerZ: Float) {
        val dx = playerX - posX
        val dz = playerZ - posZ
        val dist = sqrt((dx * dx + dz * dz).toDouble()).toFloat()

        if (dist > 3.0f) {
            posX += (dx / dist) * 1.0f * deltaTime
            posZ += (dz / dist) * 1.0f * deltaTime
        }
    }

    fun checkHit(playerX: Float, playerZ: Float, attackRange: Float, damage: Int) {
        val dx = playerX - posX
        val dz = playerZ - posZ
        val dist = sqrt((dx * dx + dz * dz).toDouble()).toFloat()

        if (dist <= attackRange) {
            health -= damage
            posX -= (dx / dist) * 0.5f
            posZ -= (dz / dist) * 0.5f
        }
    }
}
