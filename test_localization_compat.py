import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solidworks.assembly import AssemblyTools
from solidworks.connection import SolidWorksConnection
from solidworks import selection_helpers as sel


class _FakeApp:
    RevisionNumber = "33.1.2"

    def __init__(self, defaults=None):
        self.defaults = defaults or {}

    def GetUserPreferenceStringValue(self, key):
        return self.defaults.get(key, "")


class _FakeRefPlane:
    def __init__(self, normal=None, origin=(0.0, 0.0, 0.0)):
        self.normal = normal
        self.origin = origin

    def GetRefPlaneParams(self):
        if self.normal is None:
            raise RuntimeError("plane parameters unavailable")
        return (*self.origin, 1.0, 0.0, 0.0, *self.normal)


class _FakeFeature:
    def __init__(self, name, normal=None, feature_type="RefPlane"):
        self.Name = name
        self._type = feature_type
        self._specific = _FakeRefPlane(normal)
        self._next = None
        self.selected = None

    def GetTypeName2(self):
        return self._type

    def GetSpecificFeature2(self):
        return self._specific

    def GetNextFeature(self):
        return self._next

    def Select2(self, append, mark):
        self.selected = (append, mark)
        return True


class _FakeDoc:
    def __init__(self, features):
        self.features = list(features)
        for current, following in zip(self.features, self.features[1:]):
            current._next = following
        self.cleared = False

    def FirstFeature(self):
        return self.features[0] if self.features else None

    def FeatureByName(self, name):
        return next((f for f in self.features if f.Name == name), None)

    def ClearSelection2(self, _all):
        self.cleared = True


class _FakeComponent:
    def __init__(self, name, model):
        self.Name2 = name
        self._model = model

    def GetModelDoc2(self):
        return self._model


class _FakeAssemblyExtension:
    def __init__(self, accepted_name):
        self.accepted_name = accepted_name
        self.attempted = []

    def SelectByID2(self, name, *_args):
        self.attempted.append(name)
        return name == self.accepted_name


class _FakeAssemblyDoc:
    GetTitle = "测试装配体.SLDASM"

    def __init__(self, component, accepted_name):
        self.component = component
        self.Extension = _FakeAssemblyExtension(accepted_name)

    def GetComponentByName(self, name):
        return self.component if name == self.component.Name2 else None


class TemplateDiscoveryTests(unittest.TestCase):
    def test_prefers_current_version_root_gb_templates_over_mbd(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "SOLIDWORKS" / "SOLIDWORKS 2025" / "templates"
            (base / "MBD").mkdir(parents=True)
            part = base / "gb_part.prtdot"
            assembly = base / "gb_assembly.asmdot"
            drawing = base / "gb_a4.drwdot"
            part.touch()
            assembly.touch()
            drawing.touch()
            (base / "MBD" / "part 0001mm and smaller.prtdot").touch()

            connection = SolidWorksConnection()
            connection.app = _FakeApp({
                8: "~BLANK_PART_TEMPLATE.prtdot",
                9: "~BLANK_ASSY_TEMPLATE.asmdot",
                10: str(drawing),
            })
            with patch.dict(os.environ, {"PROGRAMDATA": temp}):
                self.assertEqual(connection.find_template(), str(part))
                self.assertEqual(connection.find_assembly_template(), str(assembly))
                self.assertEqual(connection.find_drawing_template(), str(drawing))

    def test_uses_existing_user_default_before_scanning(self):
        with tempfile.TemporaryDirectory() as temp:
            custom = Path(temp) / "custom.prtdot"
            custom.touch()
            connection = SolidWorksConnection()
            connection.app = _FakeApp({8: str(custom)})
            with patch.dict(os.environ, {"PROGRAMDATA": str(Path(temp) / "missing")}):
                self.assertEqual(connection.find_template(), str(custom))


class StandardPlaneResolutionTests(unittest.TestCase):
    def test_resolves_shuffled_localized_planes_by_geometry(self):
        right = _FakeFeature("任意右侧名称", (1.0, 0.0, 0.0))
        front = _FakeFeature("任意前侧名称", (0.0, 0.0, 1.0))
        top = _FakeFeature("任意顶部名称", (0.0, 1.0, 0.0))
        doc = _FakeDoc([right, front, top])

        self.assertIs(sel.resolve_plane_feature(doc, "Front"), front)
        self.assertIs(sel.resolve_plane_feature(doc, "Top"), top)
        self.assertIs(sel.resolve_plane_feature(doc, "Right"), right)
        self.assertEqual(
            sel.get_standard_plane_names(doc),
            {"Front": front.Name, "Top": top.Name, "Right": right.Name},
        )

    def test_uses_simplified_chinese_names_when_geometry_is_unavailable(self):
        front = _FakeFeature("前视基准面")
        top = _FakeFeature("上视基准面")
        right = _FakeFeature("右视基准面")
        doc = _FakeDoc([top, right, front])

        self.assertIs(sel.resolve_plane_feature(doc, "Front"), front)
        self.assertIs(sel.resolve_plane_feature(doc, "Top"), top)
        self.assertIs(sel.resolve_plane_feature(doc, "Right"), right)

    def test_select_plane_with_mark_selects_resolved_feature(self):
        front = _FakeFeature("前侧参考", (0.0, 0.0, 1.0))
        doc = _FakeDoc([front])

        self.assertTrue(sel.select_plane_with_mark(doc, "Front", mark=2))
        self.assertTrue(doc.cleared)
        self.assertEqual(front.selected, (False, 2))


class AssemblyPlaneResolutionTests(unittest.TestCase):
    def test_mate_uses_component_localized_plane_name_first(self):
        component_doc = _FakeDoc([
            _FakeFeature("前视基准面", (0.0, 0.0, 1.0)),
            _FakeFeature("上视基准面", (0.0, 1.0, 0.0)),
            _FakeFeature("右视基准面", (1.0, 0.0, 0.0)),
        ])
        component = _FakeComponent("方块-1", component_doc)
        qualified = "前视基准面@方块-1@测试装配体"
        assembly_doc = _FakeAssemblyDoc(component, qualified)
        tools = AssemblyTools(connection=None)

        selected = tools._select_mate_entity(
            assembly_doc,
            {"plane": "Front", "component": "方块-1"},
            append=False,
        )

        self.assertEqual(selected, "前视基准面@方块-1")
        self.assertEqual(assembly_doc.Extension.attempted, [qualified])


if __name__ == "__main__":
    unittest.main()
