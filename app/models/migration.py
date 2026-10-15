from enum import Enum
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, model_validator
from app.models.schema import DatabaseSchemaMetadata


class TableRole(str, Enum):
    TARGET_INSERT = "TARGET_INSERT"
    LOOKUP_ONLY = "LOOKUP_ONLY"
    DEPENDENCY_INSERT = "DEPENDENCY_INSERT"
    SKIP = "SKIP"


class ColumnMappingResult(BaseModel):
    excel_column: str = Field(..., alias="excelColumn")
    database_table: str = Field(..., alias="databaseTable")
    database_column: str = Field(..., alias="databaseColumn")
    normalized_source_name: Optional[str] = Field(None, alias="normalizedSourceName")
    normalized_target_name: Optional[str] = Field(None, alias="normalizedTargetName")
    business_concept: Optional[str] = Field(None, alias="businessConcept")
    source_data_type: Optional[str] = Field(None, alias="sourceDataType")
    target_data_type: Optional[str] = Field(None, alias="targetDataType")
    explanation: Optional[str] = Field(None, alias="explanation")
    confidence: float = Field(1.0, alias="confidence")
    match_type: str = Field("EXACT", alias="matchType")
    status: str = Field("MATCHED", alias="status")
    requirement_type: Optional[str] = Field(None, alias="requirementType")
    is_required: Optional[bool] = Field(None, alias="isRequired")
    candidates: List[Dict[str, Any]] = Field(default_factory=list, alias="candidates")

    class Config:
        populate_by_name = True
        by_alias = True



class ValueResolutionResult(BaseModel):
    excel_column: str = Field(..., alias="excelColumn")
    excel_value: str = Field(..., alias="excelValue")
    reference_table: str = Field(..., alias="referenceTable")
    target_column: str = Field(..., alias="targetColumn")
    resolved_id: Optional[str] = Field(None, alias="resolvedId")
    status: str = Field("MATCHED", alias="status")
    excel_name: Optional[str] = Field(None, alias="excelName")
    matched_database_name: Optional[str] = Field(None, alias="matchedDatabaseName")
    value_source: str = Field("DATABASE_LOOKUP", alias="valueSource")
    validation_status: str = Field("VALID", alias="validationStatus")
    candidates: List[Dict[str, Any]] = Field(default_factory=list, alias="candidates")
    message: Optional[str] = Field(None, alias="message")

    class Config:
        populate_by_name = True
        by_alias = True


class SourceRowPreviewItem(BaseModel):
    """
    Detailed row-level column plan for the migration preview.
    - Source file and Excel/CSV row number.
    - Target database, schema, and table.
    - Target column name.
    - Original source column and value.
    - Final value that will be sent to SQL Server.
    - Value source: EXCEL, EXISTING_DATABASE, FOREIGN_KEY, GENERATED_ID, DATABASE_DEFAULT, NULL, USER_OVERRIDE, or DATABASE_LOOKUP.
    - Mapping status and confidence.
    - Column flags: nullable, required, identity, has_default.
    - Any validation warnings or errors.
    """
    source_row: int = Field(..., alias="sourceRow")
    source_file: str = Field(..., alias="sourceFile")
    target_database: str = Field(..., alias="targetDatabase")
    target_schema: str = Field("dbo", alias="targetSchema")
    target_table: str = Field(..., alias="targetTable")
    target_column: str = Field(..., alias="targetColumn")
    source_column: Optional[str] = Field(None, alias="sourceColumn")
    source_value: Optional[str] = Field(None, alias="sourceValue")
    final_value: Optional[str] = Field(None, alias="finalValue")
    value_source: str = Field("EXCEL", alias="valueSource")
    mapping_status: str = Field("MATCHED", alias="mappingStatus")
    mapping_confidence: float = Field(1.0, alias="mappingConfidence")
    is_nullable: bool = Field(True, alias="isNullable")
    is_required: bool = Field(False, alias="isRequired")
    is_identity: bool = Field(False, alias="isIdentity")
    has_default: bool = Field(False, alias="hasDefault")
    requirement_type: str = Field("OPTIONAL", alias="requirementType")
    validation_status: str = Field("VALID", alias="validationStatus")
    validation_message: Optional[str] = Field(None, alias="validationMessage")

    class Config:
        populate_by_name = True
        by_alias = True

    def __getattr__(self, item: str):
        if item == "final_sql_value":
            return self.final_value
        for field_name, field_info in self.__class__.model_fields.items():
            if field_info.alias == item:
                return getattr(self, field_name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{item}'")


class ForeignKeyPlanItem(BaseModel):
    """
    Dependency and foreign-key plan item:
    - Child table and column.
    - Parent table and referenced column.
    - Source business-key value used for lookup.
    - Existing parent record found, if any.
    - Resolved database ID, if known.
    - Generated ID, if pending insertion.
    - Resolution status: MATCHED, NOT_FOUND, AMBIGUOUS, or PENDING.
    - Planned insertion order.
    """
    child_table: str = Field(..., alias="childTable")
    child_column: str = Field(..., alias="childColumn")
    parent_table: str = Field(..., alias="parentTable")
    parent_column: str = Field(..., alias="parentColumn")
    source_business_key: Optional[str] = Field(None, alias="sourceBusinessKey")
    existing_parent_found: bool = Field(False, alias="existingParentFound")
    resolved_id: Optional[str] = Field(None, alias="resolvedId")
    generated_id_pending: bool = Field(False, alias="generatedIdPending")
    status: str = Field("PENDING", alias="status")
    insertion_order: int = Field(1, alias="insertionOrder")
    matched_database_name: Optional[str] = Field(None, alias="matchedDatabaseName")
    value_source: str = Field("DATABASE_LOOKUP", alias="valueSource")
    validation_status: str = Field("VALID", alias="validationStatus")
    candidates: List[Dict[str, Any]] = Field(default_factory=list, alias="candidates")
    master_price: Optional[float] = Field(None, alias="masterPrice")
    final_price: Optional[float] = Field(None, alias="finalPrice")

    class Config:
        populate_by_name = True
        by_alias = True


class ItemPriceResolutionItem(BaseModel):
    """
    Tracks item resolution and price mapping from ItemsMaster to OrderLineItems:
    - Excel item name
    - Matched ItemsMaster.Item_ID
    - ItemsMaster.Item_Price
    - Final OrderLineItems.Item_Price
    - Match status
    """
    excel_item_name: str = Field(..., alias="excelItemName")
    matched_item_id: Optional[str] = Field(None, alias="matchedItemId")
    master_price: Optional[float] = Field(None, alias="masterPrice")
    final_order_line_price: Optional[float] = Field(None, alias="finalOrderLinePrice")
    match_status: str = Field("MATCHED", alias="matchStatus")
    source_row: Optional[int] = Field(None, alias="sourceRow")
    source_price: Optional[float] = Field(None, alias="sourcePrice")
    item_group: Optional[str] = Field(None, alias="itemGroup")

    class Config:
        populate_by_name = True
        by_alias = True


class ResolvedEntityRecord(BaseModel):
    """
    Retains the complete matched database record and metadata:
    - Actual referenced table
    - Actual primary key or unique key
    - Actual database-generated ID
    - Matched business fields such as outlet name, location, code, or address
    - Matching method and confidence
    - Status: RESOLVED, AMBIGUOUS, UNRESOLVED, NOT_FOUND
    """
    status: str = Field("RESOLVED", alias="status")
    matched_table: str = Field(..., alias="matchedTable")
    matched_primary_key_column: str = Field(..., alias="matchedPrimaryKeyColumn")
    matched_primary_key_value: Any = Field(..., alias="matchedPrimaryKeyValue")
    matched_by: List[str] = Field(default_factory=list, alias="matchedBy")
    confidence: float = Field(1.0, alias="confidence")
    record_data: Dict[str, Any] = Field(default_factory=dict, alias="recordData")
    match_method: str = Field("DATABASE_LOOKUP", alias="matchMethod")
    message: Optional[str] = Field(None, alias="message")
    candidates: List[Dict[str, Any]] = Field(default_factory=list, alias="candidates")

    class Config:
        populate_by_name = True
        by_alias = True

    def __getattr__(self, item: str):
        for field_name, field_info in self.__class__.model_fields.items():
            if field_info.alias == item:
                return getattr(self, field_name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{item}'")


class ValueResolutionSummary(BaseModel):
    values_inspected: int = Field(0, alias="valuesInspected")
    values_matched_existing: int = Field(0, alias="valuesMatchedExisting")
    values_resolved_confirmed: int = Field(0, alias="valuesResolvedConfirmed")
    values_requiring_new_parent: int = Field(0, alias="valuesRequiringNewParent")
    unresolved_foreign_keys: int = Field(0, alias="unresolvedForeignKeys")
    ambiguous_values: int = Field(0, alias="ambiguousValues")
    values_rejected_validation: int = Field(0, alias="valuesRejectedValidation")

    class Config:
        populate_by_name = True
        by_alias = True


class ColumnOverrideItem(BaseModel):
    excel_column: str = Field(..., alias="excelColumn")
    database_table: str = Field(..., alias="databaseTable")
    database_column: str = Field(..., alias="databaseColumn")
    action: str = Field("MAP", alias="action")  # "MAP", "DO_NOT_MIGRATE", "AUTO"
    business_concept: Optional[str] = Field(None, alias="businessConcept")
    save_rule: bool = Field(False, alias="saveRule")

    class Config:
        populate_by_name = True
        by_alias = True


class MappingOverridesRequest(BaseModel):
    overrides: List[ColumnOverrideItem] = Field(default_factory=list, alias="overrides")

    class Config:
        populate_by_name = True
        by_alias = True


class ValueMappingItem(BaseModel):
    excel_column: str = Field(..., alias="excelColumn")
    database_table: str = Field(..., alias="databaseTable")
    database_column: str = Field(..., alias="databaseColumn")
    excel_value: str = Field(..., alias="excelValue")
    normalized_excel_value: Optional[str] = Field(None, alias="normalizedExcelValue")
    target_value: Optional[str] = Field(None, alias="targetValue")
    match_type: str = Field("EXACT", alias="matchType")
    status: str = Field("MATCHED", alias="status")
    confidence: float = Field(1.0, alias="confidence")
    explanation: Optional[str] = Field(None, alias="explanation")
    candidates: List[Dict[str, Any]] = Field(default_factory=list, alias="candidates")
    occurrence_count: int = Field(1, alias="occurrenceCount")
    matched_table: Optional[str] = Field(None, alias="matchedTable")
    matched_primary_key_column: Optional[str] = Field(None, alias="matchedPrimaryKeyColumn")
    matched_primary_key_value: Optional[Any] = Field(None, alias="matchedPrimaryKeyValue")
    matched_record_data: Optional[Dict[str, Any]] = Field(None, alias="matchedRecordData")

    class Config:
        populate_by_name = True
        by_alias = True


class ValueOverrideItem(BaseModel):
    excel_column: str = Field(..., alias="excelColumn")
    database_table: str = Field(..., alias="databaseTable")
    database_column: str = Field(..., alias="databaseColumn")
    excel_value: str = Field(..., alias="excelValue")
    target_value: str = Field(..., alias="targetValue")
    save_cache: bool = Field(True, alias="saveCache")

    class Config:
        populate_by_name = True
        by_alias = True


class ValueOverridesRequest(BaseModel):
    overrides: List[ValueOverrideItem] = Field(default_factory=list, alias="overrides")

    class Config:
        populate_by_name = True
        by_alias = True


class RowValidationError(BaseModel):
    row_number: int = Field(..., alias="rowNumber")
    table_name: str = Field(..., alias="tableName")
    column_name: str = Field(..., alias="columnName")
    source_column: Optional[str] = Field(None, alias="sourceColumn")
    invalid_value: Optional[str] = Field(None, alias="invalidValue")
    error_type: str = Field(..., alias="errorType")
    message: str = Field(..., alias="message")
    recommended_correction: Optional[str] = Field(None, alias="recommendedCorrection")
    severity: str = Field("ERROR", alias="severity")

    class Config:
        populate_by_name = True
        by_alias = True


class ValidationSummary(BaseModel):
    total_rows: int = Field(0, alias="totalRows")
    valid_rows: int = Field(0, alias="validRows")
    invalid_rows: int = Field(0, alias="invalidRows")
    warnings_count: int = Field(0, alias="warningsCount")
    matched_columns_count: int = Field(0, alias="matchedColumnsCount")
    unmatched_columns_count: int = Field(0, alias="unmatchedColumnsCount")
    unresolved_foreign_keys_count: int = Field(0, alias="unresolvedForeignKeysCount")
    errors: List[RowValidationError] = Field(default_factory=list, alias="errors")
    warnings: List[RowValidationError] = Field(default_factory=list, alias="warnings")
    circular_dependencies: List[str] = Field(default_factory=list, alias="circularDependencies")
    can_execute: bool = Field(True, alias="canExecute")

    class Config:
        populate_by_name = True
        by_alias = True

    def __getattr__(self, item: str):
        for field_name, field_info in self.__class__.model_fields.items():
            if field_info.alias == item:
                return getattr(self, field_name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{item}'")



class PlannedOperationsSummary(BaseModel):
    total_planned_inserts: int = Field(0, alias="totalPlannedInserts")
    total_planned_updates: int = Field(0, alias="totalPlannedUpdates")
    total_planned_skips: int = Field(0, alias="totalPlannedSkips")
    planned_inserts_by_table: Dict[str, int] = Field(default_factory=dict, alias="plannedInsertsByTable")
    planned_updates_by_table: Dict[str, int] = Field(default_factory=dict, alias="plannedUpdatesByTable")
    planned_skips_by_table: Dict[str, int] = Field(default_factory=dict, alias="plannedSkipsByTable")
    table_roles: Dict[str, str] = Field(default_factory=dict, alias="tableRoles")
    skipped_tables: Dict[str, str] = Field(default_factory=dict, alias="skippedTables")
    target_insert_tables: List[str] = Field(default_factory=list, alias="targetInsertTables")
    lookup_only_tables: List[str] = Field(default_factory=list, alias="lookupOnlyTables")
    dependency_insert_tables: List[str] = Field(default_factory=list, alias="dependencyInsertTables")
    statement: str = Field("This preview has not written any data to the database.", alias="statement")
    can_execute: bool = Field(True, alias="canExecute")

    class Config:
        populate_by_name = True
        by_alias = True


class Step3ProcessingResponse(BaseModel):
    upload_id: str = Field(..., alias="uploadId")
    status: str = Field("COMPLETED", alias="status")
    schema_metadata: DatabaseSchemaMetadata = Field(..., alias="schema")
    column_mappings: List[ColumnMappingResult] = Field(default_factory=list, alias="columnMappings")
    value_resolutions: List[ValueResolutionResult] = Field(default_factory=list, alias="valueResolutions")
    value_mappings: List[ValueMappingItem] = Field(default_factory=list, alias="valueMappings")
    value_resolution_summary: Optional[ValueResolutionSummary] = Field(default=None, alias="valueResolutionSummary")
    tables_processed: List[str] = Field(default_factory=list, alias="tablesProcessed")
    table_roles: Dict[str, str] = Field(default_factory=dict, alias="tableRoles")
    skipped_tables: Dict[str, str] = Field(default_factory=dict, alias="skippedTables")
    target_insert_tables: List[str] = Field(default_factory=list, alias="targetInsertTables")
    lookup_only_tables: List[str] = Field(default_factory=list, alias="lookupOnlyTables")
    dependency_insert_tables: List[str] = Field(default_factory=list, alias="dependencyInsertTables")
    matched_columns_count: int = Field(0, alias="matchedColumnsCount")
    matched_values_count: int = Field(0, alias="matchedValuesCount")
    unmatched_values_count: int = Field(0, alias="unmatchedValuesCount")
    ambiguous_values_count: int = Field(0, alias="ambiguousValuesCount")
    processing_time_seconds: float = Field(0.0, alias="processingTimeSeconds")

    # New Safe Preview & Validation Fields
    preview_rows: List[SourceRowPreviewItem] = Field(default_factory=list, alias="previewRows")
    dependency_plan: List[ForeignKeyPlanItem] = Field(default_factory=list, alias="dependencyPlan")
    validation_summary: Optional[ValidationSummary] = Field(default=None, alias="validationSummary")
    planned_operations: Optional[PlannedOperationsSummary] = Field(default=None, alias="plannedOperations")
    execution_order: List[str] = Field(default_factory=list, alias="executionOrder")
    statement: str = Field("This preview has not written any data to the database.", alias="statement")
    can_execute: bool = Field(True, alias="canExecute")
    item_price_resolutions: List[ItemPriceResolutionItem] = Field(default_factory=list, alias="itemPriceResolutions")


    class Config:
        populate_by_name = True
        by_alias = True


class TableMigrationStat(BaseModel):
    inserted: int = 0
    updated: int = 0
    reused: int = 0
    skipped: int = 0
    failed: int = 0
    verified_inserted: int = Field(0, alias="verifiedInserted")
    total_table_rows: int = Field(0, alias="totalTableRows")

    class Config:
        populate_by_name = True
        by_alias = True


class MigrationExecutionResponse(BaseModel):
    upload_id: str = Field(..., alias="uploadId")
    status: str = Field("COMPLETED", alias="status")
    transaction: str = Field("COMMITTED", alias="transaction")
    transaction_status: Optional[str] = Field("COMMITTED", alias="transactionStatus")
    can_execute: bool = Field(True, alias="canExecute")
    execution_started: Optional[str] = Field(None, alias="executionStarted")
    execution_completed: Optional[str] = Field(None, alias="executionCompleted")
    tables: Dict[str, TableMigrationStat] = Field(default_factory=dict)
    
    # Source row metrics (Counting unit = Source Excel/CSV rows)
    source_rows_total: int = Field(0, alias="sourceRowsTotal")
    source_rows_with_inserts: int = Field(0, alias="sourceRowsWithInserts")
    source_rows_fully_reused: int = Field(0, alias="sourceRowsFullyReused")
    source_rows_failed: int = Field(0, alias="sourceRowsFailed")

    # Target table operations metrics (Counting unit = Target table records/operations)
    total_table_operations: int = Field(0, alias="totalTableOperations")
    total_inserted: int = Field(0, alias="totalInserted")
    total_updated: int = Field(0, alias="totalUpdated")
    total_reused: int = Field(0, alias="totalReused")
    total_skipped: int = Field(0, alias="totalSkipped")
    total_failed: int = Field(0, alias="totalFailed")
    inserted_count: int = Field(0, alias="insertedCount")
    updated_count: int = Field(0, alias="updatedCount")
    reused_count: int = Field(0, alias="reusedCount")
    skipped_count: int = Field(0, alias="skippedCount")
    failed_count: int = Field(0, alias="failedCount")

    # Verification and FK propagation details
    verified_inserted_counts: Dict[str, int] = Field(default_factory=dict, alias="verifiedInsertedCounts")
    foreign_keys_propagated: int = Field(0, alias="foreignKeysPropagated")
    verification_details: Dict[str, str] = Field(default_factory=dict, alias="verificationDetails")

    table_roles: Dict[str, str] = Field(default_factory=dict, alias="tableRoles")
    target_tables: List[str] = Field(default_factory=list, alias="targetTables")
    lookup_tables: List[str] = Field(default_factory=list, alias="lookupTables")
    dependency_insert_tables: List[str] = Field(default_factory=list, alias="dependencyInsertTables")
    skipped_tables: Dict[str, str] = Field(default_factory=dict, alias="skippedTables")

    validation_errors: List[RowValidationError] = Field(default_factory=list, alias="validationErrors")
    warnings: List[RowValidationError] = Field(default_factory=list, alias="warnings")
    generated_ids: Dict[str, List[Any]] = Field(default_factory=dict, alias="generatedIds")
    failure_details: List[Dict[str, Any]] = Field(default_factory=list, alias="failureDetails")
    verification_counts: Dict[str, int] = Field(default_factory=dict, alias="verificationCounts")
    processing_time_seconds: float = Field(0.0, alias="processingTimeSeconds")
    message: str = Field("", alias="message")
    error_report: Optional[List[Dict[str, Any]]] = Field(default=None, alias="errorReport")

    class Config:
        populate_by_name = True
        by_alias = True

    def __getattr__(self, item: str):
        for field_name, field_info in self.__class__.model_fields.items():
            if field_info.alias == item:
                return getattr(self, field_name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{item}'")


    @model_validator(mode="after")
    def sync_aliases(self) -> "MigrationExecutionResponse":
        if self.transaction_status is None:
            self.transaction_status = self.transaction
        if not self.inserted_count and self.total_inserted:
            self.inserted_count = self.total_inserted
        if not self.updated_count and self.total_updated:
            self.updated_count = self.total_updated
        if not self.reused_count and self.total_reused:
            self.reused_count = self.total_reused
        if not self.skipped_count and self.total_skipped:
            self.skipped_count = self.total_skipped
        if not self.failed_count and self.total_failed:
            self.failed_count = self.total_failed
        if not self.total_table_operations and (self.total_inserted or self.total_reused):
            self.total_table_operations = self.total_inserted + self.total_reused
        return self


class MigrationJobStatus(BaseModel):
    job_id: str = Field(..., alias="jobId")
    upload_id: str = Field(..., alias="uploadId")
    status: str = Field("PENDING", alias="status")
    transaction_status: str = Field("NOT_EXECUTED", alias="transactionStatus")
    current_table: Optional[str] = Field(None, alias="currentTable")
    total_tables: int = Field(0, alias="totalTables")
    tables_completed: int = Field(0, alias="tablesCompleted")
    total_rows: int = Field(0, alias="totalRows")
    rows_processed: int = Field(0, alias="rowsProcessed")
    
    # Source row metrics
    source_rows_total: int = Field(0, alias="sourceRowsTotal")
    source_rows_with_inserts: int = Field(0, alias="sourceRowsWithInserts")
    source_rows_fully_reused: int = Field(0, alias="sourceRowsFullyReused")

    # Target table operations metrics
    total_table_operations: int = Field(0, alias="totalTableOperations")
    total_inserted: int = Field(0, alias="totalInserted")
    total_reused: int = Field(0, alias="totalReused")
    percent_complete: float = Field(0.0, alias="percentComplete")
    elapsed_seconds: float = Field(0.0, alias="elapsedSeconds")
    started_at: Optional[str] = Field(None, alias="startedAt")
    completed_at: Optional[str] = Field(None, alias="completedAt")
    message: str = Field("", alias="message")
    error_message: Optional[str] = Field(None, alias="errorMessage")
    result: Optional[MigrationExecutionResponse] = Field(None, alias="result")

    class Config:
        populate_by_name = True
        by_alias = True

    def __getattr__(self, item: str):
        for field_name, field_info in self.__class__.model_fields.items():
            if field_info.alias == item:
                return getattr(self, field_name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{item}'")

