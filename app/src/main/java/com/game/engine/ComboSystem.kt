package com.game.engine

enum class AttackState {
    IDLE, COMBO_1, COMBO_2, FINISHER
}

class ComboSystem {
    var currentState = AttackState.IDLE
        private set
    private var stateTimer = 0f

    fun onAttackPressed() {
        when (currentState) {
            AttackState.IDLE -> {
                currentState = AttackState.COMBO_1
                stateTimer = 0.4f
            }
            AttackState.COMBO_1 -> {
                currentState = AttackState.COMBO_2
                stateTimer = 0.4f
            }
            AttackState.COMBO_2 -> {
                currentState = AttackState.FINISHER
                stateTimer = 0.6f
            }
            else -> {}
        }
    }

    fun update(deltaTime: Float) {
        if (currentState != AttackState.IDLE) {
            stateTimer -= deltaTime
            if (stateTimer <= 0f) {
                currentState = AttackState.IDLE
            }
        }
    }
}
