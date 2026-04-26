"""Orders API — create a project with properties and tenants in one call."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.orm import Project, Property, Tenant
from app.schemas.models import OrderCreate, OrderOut, TenantOut

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(payload: OrderCreate, db: Session = Depends(get_db)) -> OrderOut:
    """Create a project → properties → tenants hierarchy per BRD User Story #1."""
    project = Project(name=payload.project_name)
    db.add(project)
    db.flush()

    for prop_in in payload.properties:
        prop = Property(
            project_id=project.id,
            name=prop_in.name,
            property_type=prop_in.property_type,
            address=prop_in.address,
        )
        db.add(prop)
        db.flush()
        for t_in in prop_in.tenants:
            db.add(Tenant(
                property_id=prop.id,
                name=t_in.name,
                suite_number=t_in.suite_number,
                abstract_type=t_in.abstract_type,
            ))

    db.commit()
    db.refresh(project)
    return OrderOut.model_validate(project)


@router.get("/{project_id}", response_model=OrderOut)
def get_order(project_id: str, db: Session = Depends(get_db)) -> OrderOut:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return OrderOut.model_validate(project)


@router.get("/tenants/{tenant_id}", response_model=TenantOut)
def get_tenant(tenant_id: str, db: Session = Depends(get_db)) -> TenantOut:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return TenantOut.model_validate(tenant)
