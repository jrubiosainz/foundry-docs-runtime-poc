// Infrastructure for the "customer documents at runtime" prototype.
// Scope: resource group. scripts/deploy.sh deploys it, then uploads the corpus, creates indexes,
// deploys the hosted agent with azd, and creates the File Search prompt agent.
//
// Creates:
//   - Microsoft Foundry account (AIServices) with a project, plus chat-model and embedding deployments.
//   - Storage with the two simulated document sources (docs-dms and docs-bank containers).
//   - AI Search (Basic, free semantic search) for general documents and the index route.
//   - Log Analytics + Application Insights, connected to the project.
//   - Project connections (Application Insights and AI Search) and identity roles.
// All with Entra ID: no local keys in Foundry or Storage.

targetScope = 'resourceGroup'

@description('Region for all resources. Must support hosted agents and the selected model.')
param location string = resourceGroup().location

@description('Short prefix for resource names.')
@minLength(2)
@maxLength(8)
param baseName string = 'docs'

@description('Suffix that makes resource names unique. By default, derived from the resource group.')
@maxLength(13)
param suffix string = uniqueString(resourceGroup().id)

@description('Object ID of the deploying identity (user or service principal): receives roles to upload the corpus, create indexes, and deploy agents. Empty to skip assignments.')
param principalId string = ''

@allowed(['User', 'ServicePrincipal', 'Group'])
param principalType string = 'User'

@description('Chat-model deployment used by both agents. The deployment name matches the model name.')
param chatModelName string = 'gpt-5.6-terra'
param chatModelVersion string = '2026-07-09'

@description('GlobalStandard by default. DataZoneStandard keeps processing inside the data zone (EU in European regions).')
@allowed(['GlobalStandard', 'DataZoneStandard', 'Standard'])
param chatModelSku string = 'GlobalStandard'

@description('Capacity in thousands of tokens per minute. The CAG route sends about 40,000 tokens per turn for the 75-document customer, and cached tokens also count toward this limit: increase it for concurrent conversations.')
@minValue(1)
param chatModelCapacity int = 150

param embeddingModelName string = 'text-embedding-3-large'
param embeddingModelVersion string = '1'

@allowed(['GlobalStandard', 'DataZoneStandard', 'Standard'])
param embeddingModelSku string = 'GlobalStandard'

@minValue(1)
param embeddingModelCapacity int = 150

@description('Tags for all resources (for example, those required by your organization policy).')
param tags object = {}

var allTags = union({ project: 'foundry-docs-runtime-poc', dataClassification: 'synthetic' }, tags)
var accountName = 'aif-${baseName}-${suffix}'
var projectName = 'proj-${baseName}'
var storageName = take(toLower('st${baseName}${suffix}'), 24)
var searchName = 'srch-${baseName}-${suffix}'
var containers = ['docs-dms', 'docs-bank']

// Built-in roles (fixed GUID in every tenant).
var roles = {
  foundryUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d' // Foundry User (formerly "Azure AI User")
  openAiContributor: 'a001fd3d-188f-4b5d-821b-7da978bf7442' // Cognitive Services OpenAI Contributor
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageBlobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
}

// ---------------------------------------------------------------------------------------------
// Observability
// ---------------------------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${baseName}-${suffix}'
  location: location
  tags: allTags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${baseName}-${suffix}'
  location: location
  tags: allTags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---------------------------------------------------------------------------------------------
// Microsoft Foundry: account, project, and models
// ---------------------------------------------------------------------------------------------

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: allTags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: accountName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow' }
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: allTags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Customer documents at runtime'
    description: 'Prototype: hosted agent (MAF) and prompt agent with File Search retrieve customer documents when the session starts.'
  }
}

// The account does not support simultaneous operations on child resources (RequestConflict): project, deployments,
// and connections are created in sequence.
resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: chatModelName
  sku: {
    name: chatModelSku
    capacity: chatModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModelName
      version: chatModelVersion
    }
  }
  dependsOn: [project]
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: embeddingModelName
  sku: {
    name: embeddingModelSku
    capacity: embeddingModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
  }
  dependsOn: [chatDeployment]
}

// ---------------------------------------------------------------------------------------------
// Simulated document sources and AI Search
// ---------------------------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: allTags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    // The prototype does not use a private network: the hosted agent and console read blobs through the public endpoint with Entra ID.
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for name in containers: {
    parent: blobService
    name: name
    properties: { publicAccess: 'None' }
  }
]

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: searchName
  location: location
  tags: allTags
  sku: { name: 'basic' }
  identity: { type: 'SystemAssigned' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    semanticSearch: 'free'
    publicNetworkAccess: 'enabled'
    authOptions: {
      aadOrApiKey: { aadAuthFailureMode: 'http401WithBearerChallenge' }
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Project connections
// ---------------------------------------------------------------------------------------------

// Application Insights: project-agent traces (without message content).
resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'appinsights'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
  }
  dependsOn: [embeddingDeployment]
}

// AI Search: the prompt agent general-documents tool (Option 2) uses this connection with Entra ID.
resource searchConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: 'aisearch'
  properties: {
    category: 'CognitiveSearch'
    target: 'https://${search.name}.search.windows.net'
    authType: 'AAD'
    isSharedToAll: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: search.id
      location: location
    }
  }
  dependsOn: [appInsightsConnection]
}

// ---------------------------------------------------------------------------------------------
// Roles
// ---------------------------------------------------------------------------------------------

// Deployer: upload the corpus, create and index in AI Search, and deploy and use the agents.
resource userFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(account.id, principalId, roles.foundryUser)
  scope: account
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.foundryUser)
  }
}

resource userOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(account.id, principalId, roles.openAiContributor)
  scope: account
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiContributor)
  }
}

resource userBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(storage.id, principalId, roles.storageBlobDataContributor)
  scope: storage
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
  }
}

resource userSearchData 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(search.id, principalId, roles.searchIndexDataContributor)
  scope: search
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataContributor)
  }
}

resource userSearchService 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(search.id, principalId, roles.searchServiceContributor)
  scope: search
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
  }
}

// Project and account identities: the prompt agent AI Search and File Search tools run with them.
// The hosted-agent identity is created when it is deployed; scripts/grant_agent_rbac.py assigns its roles.
var managedIdentities = ['project', 'account']

resource miSearchData 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for mi in managedIdentities: {
    name: guid(search.id, mi, roles.searchIndexDataReader)
    scope: search
    properties: {
      principalId: mi == 'project' ? project.identity.principalId : account.identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
    }
  }
]

resource miSearchService 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for mi in managedIdentities: {
    name: guid(search.id, mi, roles.searchServiceContributor)
    scope: search
    properties: {
      principalId: mi == 'project' ? project.identity.principalId : account.identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
    }
  }
]

resource miBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for mi in managedIdentities: {
    name: guid(storage.id, mi, roles.storageBlobDataReader)
    scope: storage
    properties: {
      principalId: mi == 'project' ? project.identity.principalId : account.identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataReader)
    }
  }
]

// ---------------------------------------------------------------------------------------------
// Outputs: scripts/deploy.sh writes them to .env and the azd environment
// ---------------------------------------------------------------------------------------------

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroup().name
output AZURE_AI_ACCOUNT_NAME string = account.name
output AZURE_AI_PROJECT_NAME string = project.name
output AZURE_AI_PROJECT_ID string = project.id
output AZURE_AI_PROJECT_ENDPOINT string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'
output AZURE_OPENAI_ENDPOINT string = 'https://${accountName}.openai.azure.com'
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = chatDeployment.name
output EMBEDDING_DEPLOYMENT_NAME string = embeddingDeployment.name
output STORAGE_ACCOUNT_NAME string = storage.name
output STORAGE_ACCOUNT_URL string = 'https://${storage.name}.blob.${environment().suffixes.storage}'
output SEARCH_SERVICE_NAME string = search.name
output SEARCH_ENDPOINT string = 'https://${search.name}.search.windows.net'
output SEARCH_CONNECTION_NAME string = searchConnection.name
output APPLICATIONINSIGHTS_CONNECTION_STRING string = appInsights.properties.ConnectionString
