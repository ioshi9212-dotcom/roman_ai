"""Authenticated HTTP management surface for the visual-reference library.

Intentionally separate from GPT Actions/turn context: binary uploads and images
must not become novel canon or turn persistence metadata.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import visual_library as library


router = APIRouter(prefix="/visual-library", tags=["Visual library (admin)"])


def require_admin(
    x_visual_library_key: str | None = Header(default=None, alias="X-Visual-Library-Key"),
) -> None:
    try:
        library.require_visual_key(x_visual_library_key)
    except library.VisualLibraryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _http_error(exc: library.VisualLibraryError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/{novel_id}/references", operation_id="listVisualReferences")
def list_visual_references(novel_id: str, subject_type: str | None = None,
                           subject_id: str | None = None, _: None = Depends(require_admin)):
    try:
        return {"novel_id": novel_id, "references": library.list_references(
            novel_id, subject_type=subject_type, subject_id=subject_id)}
    except library.VisualLibraryError as exc:
        raise _http_error(exc) from exc


@router.post("/{novel_id}/references", operation_id="uploadVisualReference", status_code=201)
def upload_visual_reference(
    novel_id: str,
    file: UploadFile = File(...),
    subject_type: str = Form(...),
    subject_id: str = Form(""),
    category: str = Form("other"),
    notes: str = Form(""),
    _: None = Depends(require_admin),
):
    try:
        raw = file.file.read(library.MAX_BYTES + 1)
        return library.save_reference(
            novel_id, raw, subject_type=subject_type, subject_id=subject_id,
            category=category, notes=notes, original_name=file.filename or "",
        )
    except library.VisualLibraryError as exc:
        raise _http_error(exc) from exc
    finally:
        file.file.close()


@router.put("/{novel_id}/references/{reference_id}", operation_id="replaceVisualReference")
def replace_visual_reference(
    novel_id: str,
    reference_id: str,
    file: UploadFile = File(...),
    subject_type: str = Form(...),
    subject_id: str = Form(""),
    category: str = Form("other"),
    notes: str = Form(""),
    _: None = Depends(require_admin),
):
    try:
        raw = file.file.read(library.MAX_BYTES + 1)
        return library.save_reference(
            novel_id, raw, subject_type=subject_type, subject_id=subject_id,
            category=category, notes=notes, original_name=file.filename or "",
            reference_id=reference_id,
        )
    except library.VisualLibraryError as exc:
        raise _http_error(exc) from exc
    finally:
        file.file.close()


@router.get("/{novel_id}/references/{reference_id}/image", operation_id="getVisualReferenceImage")
def get_visual_reference_image(
    novel_id: str, reference_id: str, _: None = Depends(require_admin),
):
    try:
        path, mime = library.get_reference_asset(novel_id, reference_id)
        return FileResponse(
            path, media_type=mime,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    except library.VisualLibraryError as exc:
        raise _http_error(exc) from exc


@router.delete("/{novel_id}/references/{reference_id}", operation_id="deleteVisualReference")
def delete_visual_reference(
    novel_id: str, reference_id: str, _: None = Depends(require_admin),
):
    try:
        return library.delete_reference(novel_id, reference_id)
    except library.VisualLibraryError as exc:
        raise _http_error(exc) from exc
