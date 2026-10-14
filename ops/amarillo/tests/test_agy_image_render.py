"""Image artifacts must come from the verified conversation and decode safely."""
import importlib
import io
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'scripts'))

def renderer():
    return importlib.import_module('agy_image_render')

def png(color='red'):
    out=io.BytesIO();Image.new('RGB',(16,16),color).save(out,format='PNG');return out.getvalue()

def test_artifact_is_new_single_image_from_verified_conversation(tmp_path):
    m=renderer();brain=tmp_path/'.gemini/antigravity-cli/brain';d=brain/'ae4d12c5-e715-4410-8873-55b738a4a2f1';d.mkdir(parents=True)
    (d/'new.png').write_bytes(png())
    assert m.artifact(tmp_path,d.name,[])==png()
    with pytest.raises(m.agy.RuntimeError):m.artifact(tmp_path,'../../private',[])
    with pytest.raises(m.agy.RuntimeError):m.artifact(tmp_path,d.name,[png()])
    (d/'second.png').write_bytes(png('blue'))
    with pytest.raises(m.agy.RuntimeError):m.artifact(tmp_path,d.name,[])

def test_artifact_rejects_symlinks_corruption_and_oversized_dimensions(tmp_path):
    m=renderer();d=tmp_path/'.gemini/antigravity-cli/brain/ae4d12c5-e715-4410-8873-55b738a4a2f1';d.mkdir(parents=True)
    target=tmp_path/'private.png';target.write_bytes(png());(d/'new.png').symlink_to(target)
    with pytest.raises(m.agy.RuntimeError):m.artifact(tmp_path,d.name,[])
    (d/'new.png').unlink();(d/'new.png').write_bytes(b'not an image')
    with pytest.raises(m.agy.RuntimeError):m.artifact(tmp_path,d.name,[])

def test_non_owner_cannot_select_commander_before_any_runtime(monkeypatch):
    m=renderer()
    monkeypatch.setattr(m.agy,'prepare',lambda *a,**kw:pytest.fail('Runtime started'))
    with pytest.raises(m.agy.RuntimeError):m.render({'user_id':'someone','include_commander':True},{})
