from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QImage, QPainter
from PyQt5.QtWidgets import QWidget

try:
    import moderngl
    import numpy as np
    RENDER_IMPORT_ERROR = None
except ImportError as error:
    moderngl = None
    np = None
    RENDER_IMPORT_ERROR = error


FIXED_POINT = 4096.0
TEXTURE_SIZE = 128

BG_VERTEX_SHADER = """
#version 330 core
in vec3 position;
in vec4 color;
out vec4 vertexColor;
void main() {
    vertexColor = color;
    gl_Position = vec4(position, 1.0);
}
"""

BG_FRAGMENT_SHADER = """
#version 330 core
in vec4 vertexColor;
out vec4 fragmentColor;
void main() {
    fragmentColor = vertexColor;
}
"""

ICON_VERTEX_SHADER = """
#version 330 core
in vec3 position;
in vec3 nextPosition;
in vec2 texCoord;
in vec3 normal;
in vec4 vertexColor;

out vec2 uv;
out vec3 transformedNormal;
out vec4 color;

uniform mat4 projection;
uniform mat4 view;
uniform mat4 model;
uniform float tween;

void main() {
    vec3 basePosition = mix(position, nextPosition, tween);
    uv = texCoord;
    transformedNormal = mat3(model) * normalize(normal);
    color = vertexColor;
    gl_Position = projection * view * model * vec4(basePosition, 1.0);
}
"""

ICON_FRAGMENT_SHADER = """
#version 330 core
#define LIGHT_COUNT 3

struct Light {
    vec4 direction;
    vec4 color;
};

in vec2 uv;
in vec3 transformedNormal;
in vec4 color;
out vec4 fragmentColor;

uniform sampler2D iconTexture;
uniform vec4 ambient;
uniform mat4 model;
uniform Light lights[LIGHT_COUNT];

void main() {
    vec3 unitNormal = normalize(transformedNormal);
    vec3 diffuse = vec3(0.0);
    for (int index = 0; index < LIGHT_COUNT; index++) {
        vec3 lightDirection = normalize((model * -lights[index].direction).xyz);
        diffuse += max(dot(lightDirection, unitNormal), 0.0)
            * lights[index].color.rgb;
    }
    vec3 textureColor = texture(iconTexture, uv).rgb;
    vec3 litColor = (ambient.rgb + diffuse) * textureColor * color.rgb;
    fragmentColor = vec4(litColor, color.a);
}
"""


@dataclass(frozen=True)
class IconSysData:
    transparency: int
    background_colors: tuple
    light_directions: tuple
    light_colors: tuple
    ambient: tuple
    normal_name: str
    copy_name: str
    delete_name: str

    @classmethod
    def parse(cls, data: bytes) -> "IconSysData":
        layout = struct.Struct("<4s2xH4xI16I28f68s64s64s64s512x")
        if len(data) < layout.size:
            raise ValueError("icon.sys is truncated")
        values = layout.unpack_from(data)
        if values[0] != b"PS2D":
            raise ValueError("icon.sys has an invalid signature")

        def filename(value: bytes) -> str:
            return value.split(b"\0", 1)[0].decode("ascii", "replace")

        return cls(
            transparency=values[2],
            background_colors=(
                values[3:7],
                values[7:11],
                values[11:15],
                values[15:19],
            ),
            light_directions=(
                values[19:23],
                values[23:27],
                values[27:31],
            ),
            light_colors=(
                values[31:35],
                values[35:39],
                values[39:43],
            ),
            ambient=values[43:47],
            normal_name=filename(values[48]),
            copy_name=filename(values[49]),
            delete_name=filename(values[50]),
        )


@dataclass
class IconData:
    shapes: object
    normals: object
    uv: object
    colors: object
    texture: bytes
    frame_length: int
    animation_speed: float

    @classmethod
    def parse(cls, data: bytes) -> "IconData":
        if np is None:
            raise RuntimeError("3D rendering dependencies are unavailable")
        magic, shape_count, texture_type, _reserved, vertex_count = struct.unpack_from(
            "<5I", data, 0
        )
        if magic != 0x010000 or not shape_count or vertex_count % 3:
            raise ValueError("Invalid PS2 3D icon header")
        offset = 20
        shapes = np.zeros((shape_count, vertex_count, 3), dtype=np.float32)
        normals = np.zeros((vertex_count, 3), dtype=np.float32)
        uv = np.zeros((vertex_count, 2), dtype=np.float32)
        colors = np.zeros((vertex_count, 4), dtype=np.float32)

        for vertex in range(vertex_count):
            for shape in range(shape_count):
                x, y, z, _w = struct.unpack_from("<3hH", data, offset)
                shapes[shape, vertex] = (x, y, z)
                offset += 8
            x, y, z, _w = struct.unpack_from("<3hH", data, offset)
            normals[vertex] = (x, y, z)
            offset += 8
            uv[vertex] = struct.unpack_from("<2h", data, offset)
            offset += 4
            red, green, blue, alpha = struct.unpack_from("<4B", data, offset)
            colors[vertex] = (
                min(1.0, red / 128.0),
                min(1.0, green / 128.0),
                min(1.0, blue / 128.0),
                1.0 if alpha == 0 else min(1.0, alpha / 128.0),
            )
            offset += 4

        animation_magic, frame_length, animation_speed, _play, frame_count = (
            struct.unpack_from("<IIfII", data, offset)
        )
        offset += 20
        if animation_magic != 1:
            raise ValueError("Invalid PS2 icon animation header")
        for _frame in range(frame_count):
            _shape_id, key_count, _key_time, _key_value = struct.unpack_from(
                "<4I", data, offset
            )
            offset += 16 + max(0, key_count - 1) * 8

        texture = b""
        if texture_type & 0x04:
            if texture_type & 0x08:
                texture = cls._decompress_texture(data, offset)
            else:
                texture = data[offset : offset + TEXTURE_SIZE * TEXTURE_SIZE * 2]
        texture = cls._decode_texture(texture)
        return cls(
            shapes=shapes / FIXED_POINT,
            normals=normals / FIXED_POINT,
            uv=uv / FIXED_POINT,
            colors=colors,
            texture=texture,
            frame_length=max(1, frame_length),
            animation_speed=max(0.01, animation_speed),
        )

    @staticmethod
    def _decompress_texture(data: bytes, offset: int) -> bytes:
        compressed_size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        end = min(len(data), offset + compressed_size)
        output = bytearray()
        while offset + 2 <= end and len(output) < TEXTURE_SIZE * TEXTURE_SIZE * 2:
            code = struct.unpack_from("<H", data, offset)[0]
            offset += 2
            if code & 0x8000:
                count = 0x10000 - code
                byte_count = min(count * 2, end - offset)
                output.extend(data[offset : offset + byte_count])
                offset += byte_count
            else:
                if offset + 2 > end:
                    break
                pixel = data[offset : offset + 2]
                offset += 2
                output.extend(pixel * code)
        return bytes(output[: TEXTURE_SIZE * TEXTURE_SIZE * 2])

    @staticmethod
    def _decode_texture(texture: bytes) -> bytes:
        output = bytearray(TEXTURE_SIZE * TEXTURE_SIZE * 3)
        pixel_count = min(len(texture) // 2, TEXTURE_SIZE * TEXTURE_SIZE)
        for index in range(pixel_count):
            value = struct.unpack_from("<H", texture, index * 2)[0]
            output[index * 3] = (value & 0x1F) << 3
            output[index * 3 + 1] = ((value >> 5) & 0x1F) << 3
            output[index * 3 + 2] = ((value >> 10) & 0x1F) << 3
        return bytes(output)


def _normalize(vector):
    length = np.linalg.norm(vector)
    return vector if length == 0 else vector / length


def _look_at(eye, target, up):
    forward = _normalize(target - eye)
    side = _normalize(np.cross(forward, up))
    upward = np.cross(side, forward)
    result = np.identity(4, dtype=np.float32)
    result[0, :3] = side
    result[1, :3] = upward
    result[2, :3] = -forward
    result[0, 3] = -np.dot(side, eye)
    result[1, 3] = -np.dot(upward, eye)
    result[2, 3] = np.dot(forward, eye)
    return result


def _perspective(field_of_view: float, aspect: float, near: float, far: float):
    scale = 1.0 / math.tan(field_of_view / 2.0)
    result = np.zeros((4, 4), dtype=np.float32)
    result[0, 0] = scale / aspect
    result[1, 1] = scale
    result[2, 2] = (far + near) / (near - far)
    result[2, 3] = (2.0 * far * near) / (near - far)
    result[3, 2] = -1.0
    return result


def _rotation_y(angle: float):
    cosine, sine = math.cos(angle), math.sin(angle)
    return np.asarray(
        (
            (cosine, 0.0, sine, 0.0),
            (0.0, 1.0, 0.0, 0.0),
            (-sine, 0.0, cosine, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
        dtype=np.float32,
    )


def _matrix_bytes(matrix) -> bytes:
    return np.ascontiguousarray(matrix.T, dtype=np.float32).tobytes()


@dataclass
class IconRenderResources:
    icon_sys: IconSysData
    icon: IconData
    vertex_buffers: list
    vertex_arrays: list
    texture: object

    def release(self) -> None:
        for vertex_array in self.vertex_arrays:
            vertex_array.release()
        for buffer in self.vertex_buffers:
            buffer.release()
        self.texture.release()
        self.vertex_arrays.clear()
        self.vertex_buffers.clear()


class OpenGLIconRenderer:
    def __init__(self) -> None:
        if moderngl is None or np is None:
            raise RuntimeError(
                f"3D rendering dependencies are unavailable: {RENDER_IMPORT_ERROR}"
            )
        errors = []
        for backend in ("egl", None):
            try:
                arguments = {"require": 330}
                if backend:
                    arguments["backend"] = backend
                self.context = moderngl.create_standalone_context(**arguments)
                break
            except Exception as error:
                errors.append(f"{backend or 'default'}: {error}")
        else:
            raise RuntimeError(
                f"Could not initialize OpenGL ({'; '.join(errors)})"
            )

        self.context.enable(
            moderngl.DEPTH_TEST | moderngl.CULL_FACE | moderngl.BLEND
        )
        self.context.blend_func = (
            moderngl.SRC_ALPHA,
            moderngl.ONE_MINUS_SRC_ALPHA,
        )
        self.background_program = self.context.program(
            vertex_shader=BG_VERTEX_SHADER,
            fragment_shader=BG_FRAGMENT_SHADER,
        )
        self.icon_program = self.context.program(
            vertex_shader=ICON_VERTEX_SHADER,
            fragment_shader=ICON_FRAGMENT_SHADER,
        )
        self.icon_sys = None
        self.icon = None
        self.vertex_buffers = []
        self.vertex_arrays = []
        self.texture = None
        self._active_resources = None
        self._render_targets = {}

    def load(self, icon_sys_path: Path, icon_path: Path) -> None:
        self.release_icon()
        resources = self.create_icon(icon_sys_path, icon_path)
        self._active_resources = resources
        self.icon_sys = resources.icon_sys
        self.icon = resources.icon
        self.vertex_buffers = resources.vertex_buffers
        self.vertex_arrays = resources.vertex_arrays
        self.texture = resources.texture

    def create_icon(
        self, icon_sys_path: Path, icon_path: Path
    ) -> IconRenderResources:
        icon_sys = IconSysData.parse(icon_sys_path.read_bytes())
        icon = IconData.parse(icon_path.read_bytes())
        vertex_buffers = []
        vertex_arrays = []
        for shape in range(len(icon.shapes)):
            next_shape = (shape + 1) % len(icon.shapes)
            vertices = np.hstack(
                (
                    icon.shapes[shape],
                    icon.shapes[next_shape],
                    icon.uv,
                    icon.normals,
                    icon.colors,
                )
            ).astype("f4")
            buffer = self.context.buffer(vertices.tobytes())
            vertex_array = self.context.vertex_array(
                self.icon_program,
                [
                    (
                        buffer,
                        "3f 3f 2f 3f 4f",
                        "position",
                        "nextPosition",
                        "texCoord",
                        "normal",
                        "vertexColor",
                    )
                ],
            )
            vertex_buffers.append(buffer)
            vertex_arrays.append(vertex_array)
        texture = self.context.texture(
            (TEXTURE_SIZE, TEXTURE_SIZE),
            3,
            icon.texture,
        )
        texture.filter = (moderngl.LINEAR, moderngl.LINEAR)
        texture.repeat_x = False
        texture.repeat_y = False
        return IconRenderResources(
            icon_sys=icon_sys,
            icon=icon,
            vertex_buffers=vertex_buffers,
            vertex_arrays=vertex_arrays,
            texture=texture,
        )

    def _background(self, resources: IconRenderResources):
        colors = resources.icon_sys.background_colors
        alpha = min(1.0, resources.icon_sys.transparency / 128.0)
        corner_colors = np.asarray(
            (
                (*np.asarray(colors[0][:3]) / 255.0, alpha),
                (*np.asarray(colors[2][:3]) / 255.0, alpha),
                (*np.asarray(colors[3][:3]) / 255.0, alpha),
                (*np.asarray(colors[1][:3]) / 255.0, alpha),
            ),
            dtype=np.float32,
        )
        positions = np.asarray(
            ((-1, 1, 0.99), (-1, -1, 0.99), (1, -1, 0.99), (1, 1, 0.99)),
            dtype=np.float32,
        )
        indices = (0, 1, 3, 2, 3, 1)
        data = np.hstack((positions[list(indices)], corner_colors[list(indices)]))
        buffer = self.context.buffer(data.astype("f4").tobytes())
        vertex_array = self.context.vertex_array(
            self.background_program,
            [(buffer, "3f 4f", "position", "color")],
        )
        return buffer, vertex_array

    def render(
        self,
        width: int,
        height: int,
        elapsed: float = 0.0,
        include_background: bool = False,
        rotation_offset: float = 0.0,
        rotation_rate: float = 0.45,
    ) -> QImage:
        if self._active_resources is None:
            return QImage()
        return self.render_icon(
            self._active_resources,
            width,
            height,
            elapsed,
            include_background,
            rotation_offset,
            rotation_rate,
        )

    def _render_target(self, width: int, height: int):
        key = (width, height)
        target = self._render_targets.get(key)
        if target is None:
            color = self.context.texture(key, 4)
            depth = self.context.depth_renderbuffer(key)
            framebuffer = self.context.framebuffer(
                color_attachments=[color], depth_attachment=depth
            )
            target = (color, depth, framebuffer)
            self._render_targets[key] = target
        return target

    def render_icon(
        self,
        resources: IconRenderResources,
        width: int,
        height: int,
        elapsed: float = 0.0,
        include_background: bool = False,
        rotation_offset: float = 0.0,
        rotation_rate: float = 0.45,
    ) -> QImage:
        color, _depth, framebuffer = self._render_target(width, height)
        framebuffer.use()
        self.context.viewport = (0, 0, width, height)
        self.context.clear(0.10, 0.11, 0.13, 0.0, depth=1.0)

        background_resources = None
        if include_background:
            background_resources = self._background(resources)
            background_resources[1].render(moderngl.TRIANGLES)

        projection = _perspective(math.radians(50.0), width / height, 0.1, 100.0)
        view = _look_at(
            np.asarray((0.0, -2.0, -10.0), dtype=np.float32),
            np.asarray((0.0, -2.0, 0.0), dtype=np.float32),
            np.asarray((0.0, -1.0, 0.0), dtype=np.float32),
        )
        model = _rotation_y(math.pi + rotation_offset + elapsed * rotation_rate)
        self.icon_program["projection"].write(_matrix_bytes(projection))
        self.icon_program["view"].write(_matrix_bytes(view))
        self.icon_program["model"].write(_matrix_bytes(model))
        self.icon_program["ambient"].value = tuple(resources.icon_sys.ambient)
        for index in range(3):
            self.icon_program[f"lights[{index}].direction"].value = tuple(
                resources.icon_sys.light_directions[index]
            )
            self.icon_program[f"lights[{index}].color"].value = tuple(
                resources.icon_sys.light_colors[index]
            )
        self.icon_program["iconTexture"].value = 0
        resources.texture.use(0)

        icon = resources.icon
        frame = int(elapsed * 60.0 * icon.animation_speed) % icon.frame_length
        frames_per_shape = icon.frame_length / len(icon.shapes)
        shape = min(len(icon.shapes) - 1, int(frame // frames_per_shape))
        tween = (frame % frames_per_shape) / frames_per_shape
        self.icon_program["tween"].value = float(tween)
        resources.vertex_arrays[shape].render(moderngl.TRIANGLES)

        raw = framebuffer.read(components=4, alignment=1)
        image = QImage(raw, width, height, width * 4, QImage.Format_RGBA8888).copy()
        image = image.mirrored(False, True)

        if background_resources:
            background_resources[1].release()
            background_resources[0].release()
        return image

    def release_icon(self) -> None:
        if self._active_resources:
            self._active_resources.release()
            self._active_resources = None
        self.vertex_arrays = []
        self.vertex_buffers = []
        self.texture = None
        self.icon = None
        self.icon_sys = None

    def release(self) -> None:
        self.release_icon()
        for color, depth, framebuffer in self._render_targets.values():
            framebuffer.release()
            depth.release()
            color.release()
        self._render_targets.clear()
        self.background_program.release()
        self.icon_program.release()
        self.context.release()


class PS2IconView(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(180, 180)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.renderer = None
        self.image = QImage()
        self.started = time.monotonic()
        self.rotation_offset = 0.0
        self.drag_x = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._next_frame)
        self.timer.start(50)

    def set_icon(self, icon_sys: Path | None, icon: Path | None) -> None:
        if not icon_sys or not icon:
            self.image = QImage()
            self.update()
            return
        try:
            if self.renderer is None:
                self.renderer = OpenGLIconRenderer()
            self.renderer.load(icon_sys, icon)
            self.started = time.monotonic()
            self._next_frame()
        except Exception:
            self.image = QImage()
            self.update()

    def _next_frame(self) -> None:
        if not self.isVisible() or self.renderer is None or self.renderer.icon is None:
            return
        width = max(180, int(self.width() * self.devicePixelRatioF()))
        height = max(180, int(self.height() * self.devicePixelRatioF()))
        self.image = self.renderer.render(
            width,
            height,
            time.monotonic() - self.started,
            include_background=False,
            rotation_offset=self.rotation_offset,
        )
        self.image.setDevicePixelRatio(self.devicePixelRatioF())
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.transparent)
        if not self.image.isNull():
            painter.drawImage(self.rect(), self.image)
        painter.end()

    def mousePressEvent(self, event) -> None:
        self.drag_x = event.x()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_x is not None:
            self.rotation_offset += (event.x() - self.drag_x) * 0.012
            self.drag_x = event.x()

    def mouseReleaseEvent(self, _event) -> None:
        self.drag_x = None

    def closeEvent(self, event) -> None:
        self.release_renderer()
        super().closeEvent(event)

    def release_renderer(self) -> None:
        if self.renderer:
            self.renderer.release()
            self.renderer = None
        self.image = QImage()
        self.update()


def render_thumbnail(icon_sys: Path, icon: Path, output: Path, size: int = 180) -> None:
    renderer = OpenGLIconRenderer()
    try:
        renderer.load(icon_sys, icon)
        image = renderer.render(
            size,
            size,
            elapsed=0.8,
            include_background=False,
            rotation_offset=-0.22,
        )
        if image.isNull() or not image.save(str(output)):
            raise RuntimeError("Could not save 3D icon thumbnail")
    finally:
        renderer.release()
