"""Where uploaded files (attachments, expense receipts) are kept.

Default: the local `instance/uploads` folder, exactly as before — nothing changes on a
PC or on a host with a persistent disk. If the GCS_BUCKET environment variable is set,
files go to that Google Cloud Storage bucket instead, which is what a host with a
throwaway disk (like Cloud Run) needs. Only the stored file name is saved in the
database either way, so both modes work through the same three functions below.
"""
import io
import os

from flask import abort, current_app, send_file, send_from_directory

GCS_BUCKET = os.environ.get("GCS_BUCKET")


def _bucket():
    from google.cloud import storage  # imported here so local installs don't need the package
    return storage.Client().bucket(GCS_BUCKET)


def _local_dir():
    path = os.path.join(current_app.instance_path, "uploads")
    os.makedirs(path, exist_ok=True)
    return path


def save_upload(file, stored_name):
    if GCS_BUCKET:
        _bucket().blob(stored_name).upload_from_file(file.stream, content_type=file.content_type, rewind=True)
    else:
        file.save(os.path.join(_local_dir(), stored_name))


def send_upload(stored_name, download_name):
    if GCS_BUCKET:
        blob = _bucket().blob(stored_name)
        if not blob.exists():
            abort(404)
        return send_file(
            io.BytesIO(blob.download_as_bytes()), as_attachment=True, download_name=download_name,
            mimetype=blob.content_type or "application/octet-stream",
        )
    return send_from_directory(_local_dir(), stored_name, as_attachment=True, download_name=download_name)


def delete_upload(stored_name):
    if GCS_BUCKET:
        blob = _bucket().blob(stored_name)
        if blob.exists():
            blob.delete()
        return
    path = os.path.join(_local_dir(), stored_name)
    if os.path.exists(path):
        os.remove(path)
