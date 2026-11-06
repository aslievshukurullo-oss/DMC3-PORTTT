package com.game.engine

import android.content.Context
import android.opengl.GLES30
import android.opengl.GLSurfaceView
import android.opengl.Matrix
import java.nio.ByteBuffer
import java.nio.ByteOrder
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10

object EngineShaders {
    val vertexSource = """
        #version 300 es
        layout(location = 0) in vec3 aPosition;
        layout(location = 1) in vec3 aNormal;
        layout(location = 2) in vec4 aBoneIndices;
        layout(location = 3) in vec4 aBoneWeights;
        
        uniform mat4 uMVPMatrix;
        uniform mat4 uModelMatrix;
        uniform mat4 uBoneMatrices[32];
        uniform vec3 uLightPos;
        
        out vec3 vColor;
        
        void main() {
            mat4 boneTransform = uBoneMatrices[int(aBoneIndices.x)] * aBoneWeights.x;
            boneTransform += uBoneMatrices[int(aBoneIndices.y)] * aBoneWeights.y;
            boneTransform += uBoneMatrices[int(aBoneIndices.z)] * aBoneWeights.z;
            boneTransform += uBoneMatrices[int(aBoneIndices.w)] * aBoneWeights.w;
            
            vec4 skinnedPosition = boneTransform * vec4(aPosition, 1.0);
            gl_Position = uMVPMatrix * skinnedPosition;
            
            vec3 skinnedNormal = normalize(mat3(uModelMatrix * boneTransform) * aNormal);
            vec3 worldPos = vec3(uModelMatrix * skinnedPosition);
            vec3 lightDir = normalize(uLightPos - worldPos);
            
            float diffuse = max(dot(skinnedNormal, lightDir), 0.2);
            vColor = vec3(0.8, 0.1, 0.1) * diffuse;
        }
    """.trimIndent()

    val fragmentSource = """
        #version 300 es
        precision mediump float;
        in vec3 vColor;
        out vec4 fragColor;
        
        void main() {
            fragColor = vec4(vColor, 1.0);
        }
    """.trimIndent()
}

class SkeletalMesh(vertices: FloatArray, normals: FloatArray, boneIndices: FloatArray, boneWeights: FloatArray, val vertexCount: Int) {
    private val vboIds = IntArray(4)
    private val vaoIds = IntArray(1)

    init {
        GLES30.glGenVertexArrays(1, vaoIds, 0)
        GLES30.glGenBuffers(4, vboIds, 0)
        GLES30.glBindVertexArray(vaoIds[0])
        setupVbo(0, 3, vertices)
        setupVbo(1, 3, normals)
        setupVbo(2, 4, boneIndices)
        setupVbo(3, 4, boneWeights)
        GLES30.glBindVertexArray(0)
    }

    private fun setupVbo(index: Int, size: Int, data: FloatArray) {
        GLES30.glBindBuffer(GLES30.GL_ARRAY_BUFFER, vboIds[index])
        val buffer = ByteBuffer.allocateDirect(data.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().put(data)
        buffer.position(0)
        GLES30.glBufferData(GLES30.GL_ARRAY_BUFFER, data.size * 4, buffer, GLES30.GL_STATIC_DRAW)
        GLES30.glEnableVertexAttribArray(index)
        GLES30.glVertexAttribPointer(index, size, GLES30.GL_FLOAT, false, 0, 0)
    }

    fun draw() {
        GLES30.glBindVertexArray(vaoIds[0])
        GLES30.glDrawArrays(GLES30.GL_TRIANGLES, 0, vertexCount)
        GLES30.glBindVertexArray(0)
    }
}

class CoreRenderer(context: Context, private val input: GameInput) : GLSurfaceView.Renderer {
    private var shaderProgram = 0
    private var uMVPMatrixLocation = 0
    private var uModelMatrixLocation = 0
    private var uBoneMatricesLocation = 0
    private var uLightPosLocation = 0

    private val boneMatrices = FloatArray(32 * 16)
    private val lightPos = floatArrayOf(5f, 15f, -5f)

    private val camera = GameCamera()
    private val player = PlayerEntity()
    private val combo = ComboSystem()
    private val dummy = EnemyDummy()
    
    private var mesh: SkeletalMesh? = null
    private var lastTime = System.nanoTime()

    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        GLES30.glClearColor(0.05f, 0.05f, 0.05f, 1.0f)
        GLES30.glEnable(GLES30.GL_DEPTH_TEST)
        GLES30.glEnable(GLES30.GL_CULL_FACE)

        val vs = GLES30.glCreateShader(GLES30.GL_VERTEX_SHADER).apply { GLES30.glShaderSource(this, EngineShaders.vertexSource); GLES30.glCompileShader(this) }
        val fs = GLES30.glCreateShader(GLES30.GL_FRAGMENT_SHADER).apply { GLES30.glShaderSource(this, EngineShaders.fragmentSource); GLES30.glCompileShader(this) }
        shaderProgram = GLES30.glCreateProgram().apply { GLES30.glAttachShader(this, vs); GLES30.glAttachShader(this, fs); GLES30.glLinkProgram(this) }

        uMVPMatrixLocation = GLES30.glGetUniformLocation(shaderProgram, "uMVPMatrix")
        uModelMatrixLocation = GLES30.glGetUniformLocation(shaderProgram, "uModelMatrix")
        uBoneMatricesLocation = GLES30.glGetUniformLocation(shaderProgram, "uBoneMatrices")
        uLightPosLocation = GLES30.glGetUniformLocation(shaderProgram, "uLightPos")

        for (i in 0 until 32) Matrix.setIdentityM(boneMatrices, i * 16)

        val vertices = floatArrayOf(
            0f, 1.5f, 0f,   -0.5f, 0f, 0.5f,   0.5f, 0f, 0.5f,
            0f, 1.5f, 0f,   0.5f, 0f, -0.5f,  -0.5f, 0f, -0.5f,
            0f, 1.5f, 0f,  -0.5f, 0f, -0.5f,  -0.5f, 0f, 0.5f,
            0f, 1.5f, 0f,   0.5f, 0f, 0.5f,   0.5f, 0f, -0.5f,
            -0.5f, 0f, 0.5f,  -0.5f, 0f, -0.5f,  0.5f, 0f, -0.5f,
            -0.5f, 0f, 0.5f,   0.5f, 0f, -0.5f,   0.5f, 0f, 0.5f
        )
        
        val normals = floatArrayOf(
            0f, 0.5f, 0.86f,  0f, 0.5f, 0.86f,  0f, 0.5f, 0.86f,
            0f, 0.5f, -0.86f, 0f, 0.5f, -0.86f, 0f, 0.5f, -0.86f,
            -0.86f, 0.5f, 0f, -0.86f, 0.5f, 0f, -0.86f, 0.5f, 0f,
            0.86f, 0.5f, 0f,  0.86f, 0.5f, 0f,  0.86f, 0.5f, 0f,
            0f, -1f, 0f,  0f, -1f, 0f,  0f, -1f, 0f,
            0f, -1f, 0f,  0f, -1f, 0f,  0f, -1f, 0f
        )

        val boneIndices = FloatArray(18 * 4) { 0f }
        val boneWeights = FloatArray(18 * 4).apply {
            for (i in 0 until 18) {
                this[i * 4] = 1.0f
            }
        }

        mesh = SkeletalMesh(
            vertices = vertices,
            normals = normals,
            boneIndices = boneIndices,
            boneWeights = boneWeights,
            vertexCount = 18
        )
    }

    override fun onSurfaceChanged(gl: GL10?, width: Int, height: Int) {
        GLES30.glViewport(0, 0, width, height)
        Matrix.perspectiveM(camera.projectionMatrix, 0, 45f, width.toFloat() / height, 0.1f, 1000f)
    }

    override fun onDrawFrame(gl: GL10?) {
        val now = System.nanoTime()
        val deltaTime = (now - lastTime) / 1_000_000_000f
        lastTime = now

        if (input.isAttackPressed) combo.onAttackPressed()
        combo.update(deltaTime)
        
        if (combo.currentState != AttackState.IDLE) {
            dummy.checkHit(player.posX, player.posZ, 2.5f, 20)
        }

        player.handleInput(input.stickX, input.stickY, camera, input.isJumpPressed, deltaTime)
        camera.update(player.posX, player.posY, player.posZ, deltaTime)
        dummy.update(deltaTime, player.posX, player.posZ)

        val wave = kotlin.math.sin(now / 150000000.0).toFloat() * 0.15f
        Matrix.setIdentityM(boneMatrices, 0)
        Matrix.translateM(boneMatrices, 0, 0f, wave, 0f)

        GLES30.glClear(GLES30.GL_COLOR_BUFFER_BIT or GLES30.GL_DEPTH_BUFFER_BIT)
        GLES30.glUseProgram(shaderProgram)

        camera.computeViewMatrix()
        player.updateMVP(camera.viewMatrix, camera.projectionMatrix)

        GLES30.glUniformMatrix4fv(uMVPMatrixLocation, 1, false, player.mvpMatrix, 0)
        GLES30.glUniformMatrix4fv(uModelMatrixLocation, 1, false, player.modelMatrix, 0)
        GLES30.glUniformMatrix4fv(uBoneMatricesLocation, 32, false, boneMatrices, 0)
        GLES30.glUniform3fv(uLightPosLocation, 1, lightPos, 0)

        mesh?.draw()
        input.resetTriggers()
    }
}
