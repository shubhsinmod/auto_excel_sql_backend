from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from app.mapping.synonyms import (
    synonym_registry,
    BusinessConcept,
    CustomMappingRule
)
from app.core.logging import logger

router = APIRouter(prefix="/api/mapping", tags=["Mapping & Synonyms"])


class SynonymAddRequest(BaseModel):
    concept_id: str = Field(..., alias="conceptId")
    synonym: str
    database: Optional[str] = None


class BusinessConceptCreateRequest(BaseModel):
    id: str
    name: str
    description: str = ""
    synonyms: List[str] = Field(default_factory=list)
    expected_type: str = Field("TEXT", alias="expectedType")
    database_scope: Optional[str] = Field(None, alias="databaseScope")


class CustomRuleCreateRequest(BaseModel):
    source_column: str = Field(..., alias="sourceColumn")
    business_concept: Optional[str] = Field(None, alias="businessConcept")
    target_table: str = Field(..., alias="targetTable")
    target_column: str = Field(..., alias="targetColumn")
    database_scope: Optional[str] = Field(None, alias="databaseScope")
    schema_scope: Optional[str] = Field("dbo", alias="schemaScope")
    status: str = "APPROVED"


# -----------------------------------------------------------------------------
# Business Concepts & Synonyms Endpoints
# -----------------------------------------------------------------------------

@router.get("/synonyms", response_model=List[BusinessConcept])
def get_all_synonyms(database: Optional[str] = Query(None)):
    """
    Returns all registered business concepts and their configured synonyms,
    filtered by database scope if provided.
    """
    return synonym_registry.get_concepts(database_scope=database)


@router.post("/synonyms")
def add_synonym(req: SynonymAddRequest):
    """
    Adds a new synonym to an existing business concept.
    """
    success = synonym_registry.add_synonym(
        concept_id=req.concept_id,
        synonym=req.synonym,
        database_scope=req.database
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Business concept '{req.concept_id}' not found."
        )
    return {"message": f"Synonym '{req.synonym}' added to concept '{req.concept_id}'."}


@router.delete("/synonyms/{concept_id}/{synonym}")
def delete_synonym(concept_id: str, synonym: str, database: Optional[str] = Query(None)):
    """
    Removes a synonym from a business concept.
    """
    removed = synonym_registry.remove_synonym(
        concept_id=concept_id,
        synonym=synonym,
        database_scope=database
    )
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Synonym '{synonym}' not found under concept '{concept_id}'."
        )
    return {"message": f"Synonym '{synonym}' removed from concept '{concept_id}'."}


@router.post("/concepts", response_model=BusinessConcept)
def create_concept(req: BusinessConceptCreateRequest):
    """
    Creates a new custom business concept with initial synonyms.
    """
    concept = BusinessConcept(
        id=req.id,
        name=req.name,
        description=req.description,
        synonyms=req.synonyms,
        expected_type=req.expected_type,
        database_scope=req.database_scope,
        is_builtin=False
    )
    return synonym_registry.add_concept(concept)


@router.delete("/concepts/{concept_id}")
def delete_concept(concept_id: str, database: Optional[str] = Query(None)):
    """
    Deletes a business concept from the registry.
    """
    deleted = synonym_registry.delete_concept(concept_id=concept_id, database_scope=database)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Business concept '{concept_id}' could not be deleted or not found."
        )
    return {"message": f"Concept '{concept_id}' deleted successfully."}


# -----------------------------------------------------------------------------
# Approved User Mapping Rules Endpoints (Learning)
# -----------------------------------------------------------------------------

@router.get("/rules", response_model=List[CustomMappingRule])
def get_approved_mapping_rules(database: Optional[str] = Query(None)):
    """
    Returns all learned and approved user mapping rules for the specified database scope.
    """
    return synonym_registry.get_approved_rules(database_scope=database)


@router.post("/rules", response_model=CustomMappingRule)
def save_approved_mapping_rule(req: CustomRuleCreateRequest):
    """
    Persists an approved user mapping rule scoped to a database.
    """
    rule = CustomMappingRule(
        source_column=req.source_column,
        business_concept=req.business_concept,
        target_table=req.target_table,
        target_column=req.target_column,
        database_scope=req.database_scope,
        schema_scope=req.schema_scope,
        status="APPROVED"
    )
    return synonym_registry.save_approved_rule(rule)


@router.delete("/rules/{rule_id}")
def delete_approved_mapping_rule(rule_id: str):
    """
    Deletes an approved user mapping rule.
    """
    deleted = synonym_registry.delete_approved_rule(rule_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Approved mapping rule '{rule_id}' not found."
        )
    return {"message": f"Mapping rule '{rule_id}' deleted successfully."}


# -----------------------------------------------------------------------------
# Value Synonyms & Cached Value Mappings Endpoints
# -----------------------------------------------------------------------------
from app.mapping.values import value_registry


class ValueSynonymAddRequest(BaseModel):
    category: str
    target_value: str = Field(..., alias="targetValue")
    synonym: str

    class Config:
        populate_by_name = True
        by_alias = True


class ValueCacheAddRequest(BaseModel):
    database_table: str = Field(..., alias="databaseTable")
    database_column: str = Field(..., alias="databaseColumn")
    source_value: str = Field(..., alias="sourceValue")
    target_value: str = Field(..., alias="targetValue")
    database_scope: Optional[str] = Field(None, alias="databaseScope")

    class Config:
        populate_by_name = True
        by_alias = True


@router.get("/values/synonyms")
def get_value_synonyms():
    """
    Returns all registered value synonyms organized by category and target value.
    """
    return value_registry.get_synonyms()


@router.post("/values/synonyms")
def add_value_synonym(req: ValueSynonymAddRequest):
    """
    Adds a new synonym for a target database value.
    """
    value_registry.add_synonym(req.category, req.target_value, req.synonym)
    return {"message": f"Synonym '{req.synonym}' added for '{req.target_value}' in '{req.category}'."}


@router.delete("/values/synonyms/{category}/{target_value}/{synonym}")
def delete_value_synonym(category: str, target_value: str, synonym: str):
    """
    Removes a synonym from a target database value.
    """
    value_registry.remove_synonym(category, target_value, synonym)
    return {"message": f"Synonym '{synonym}' removed from '{target_value}' in '{category}'."}


@router.get("/values/cache")
def get_cached_value_mappings():
    """
    Returns all cached approved value mappings scoped to target database columns.
    """
    return value_registry.get_all_cached_mappings()


@router.post("/values/cache")
def save_cached_value_mapping(req: ValueCacheAddRequest):
    """
    Caches an approved value mapping scoped to target database column for future imports.
    """
    value_registry.save_approved_mapping(
        table=req.database_table,
        column=req.database_column,
        source_value=req.source_value,
        target_value=req.target_value,
        database_scope=req.database_scope
    )
    return {"message": f"Cached value mapping: '{req.source_value}' -> '{req.target_value}' for {req.database_table}.{req.database_column}."}


@router.delete("/values/cache")
def delete_cached_value_mapping(
    database_table: str = Query(..., alias="databaseTable"),
    database_column: str = Query(..., alias="databaseColumn"),
    source_value: str = Query(..., alias="sourceValue"),
    database_scope: Optional[str] = Query(None, alias="databaseScope")
):
    """
    Removes a cached approved value mapping.
    """
    value_registry.delete_cached_mapping(
        table=database_table,
        column=database_column,
        source_value=source_value,
        database_scope=database_scope
    )
    return {"message": f"Deleted cached mapping for '{source_value}'."}
