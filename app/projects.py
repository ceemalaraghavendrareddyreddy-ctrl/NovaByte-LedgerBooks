"""Projects — a single tag list for tracking branches, jobs, or cost centers
within one company's books. See app/models.py's Project docstring for the
naming philosophy; this module is just the CRUD around that one table.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import db
from app.auth import current_company_id
from app.models import Project
from app.scoping import scoped_or_404, scoped_query

projects_bp = Blueprint("projects", __name__, url_prefix="/projects")


@projects_bp.route("")
@login_required
def project_list():
    projects = scoped_query(Project).order_by(Project.name).all()
    return render_template("projects/list.html", projects=projects)


@projects_bp.route("/new", methods=["GET", "POST"])
@login_required
def project_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Give the project a name.", "error")
            return render_template("projects/form.html", form=request.form)
        if scoped_query(Project).filter_by(name=name).first():
            flash(f"A project called '{name}' already exists.", "error")
            return render_template("projects/form.html", form=request.form)

        project = Project(company_id=current_company_id(), name=name)
        db.session.add(project)
        db.session.commit()
        flash(f"Project '{name}' created.", "success")
        return redirect(url_for("projects.project_list"))

    return render_template("projects/form.html", form={})


@projects_bp.route("/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def project_edit(project_id):
    project = scoped_or_404(Project, project_id)
    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            flash("Give the project a name.", "error")
            return render_template("projects/edit.html", project=project)
        if new_name != project.name and scoped_query(Project).filter_by(name=new_name).first():
            flash(f"A project called '{new_name}' already exists.", "error")
            return render_template("projects/edit.html", project=project)
        project.name = new_name
        db.session.commit()
        flash(f"Project '{project.name}' updated.", "success")
        return redirect(url_for("projects.project_list"))

    return render_template("projects/edit.html", project=project)


@projects_bp.route("/<int:project_id>/toggle", methods=["POST"])
@login_required
def project_toggle(project_id):
    project = scoped_or_404(Project, project_id)
    project.is_active = not project.is_active
    db.session.commit()
    flash(f"Project '{project.name}' {'activated' if project.is_active else 'deactivated'}.", "success")
    return redirect(url_for("projects.project_list"))
