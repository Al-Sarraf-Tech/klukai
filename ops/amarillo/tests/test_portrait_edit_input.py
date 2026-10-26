import base64,hashlib,io,sys
from pathlib import Path
import pytest
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'scripts'))
from portrait_edit_input import decode_source
import fal_image_render as fal


def source(size=(512,512)):
    buf=io.BytesIO();Image.new('RGB',size,'gray').save(buf,'JPEG');raw=buf.getvalue()
    return {'frame':'blink','sha256':hashlib.sha256(raw).hexdigest(),'image':base64.b64encode(raw).decode()}


def test_valid_inline_source_does_not_read_reference_files():
    value=source()
    assert fal._inputs({}, {'portrait_source':value,'include_commander':False})['klukai']==decode_source(value)


@pytest.mark.parametrize('change',[{'sha256':'0'*64},{'frame':'../base'},{'image':'!invalid'},{'image':'a'*56001},{'path':'/etc/passwd'}])
def test_bad_source_rejected_before_paid_work(change):
    value=source();value.update(change)
    with pytest.raises(ValueError):decode_source(value)


def test_wrong_dimensions_rejected():
    with pytest.raises(ValueError):decode_source(source((100,100)))
