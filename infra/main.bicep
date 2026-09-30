// Same result as scripts/setup-azure.sh, as Bicep:
//   az group create -n rg-github-backup -l westus3
//   az deployment group create -g rg-github-backup -f infra/main.bicep \
//     -p githubRepo=<owner>/<repo> userObjectId=$(az ad signed-in-user show --query id -o tsv)
param location string = resourceGroup().location
param storageAccountName string = 'ghbackup${uniqueString(resourceGroup().id)}'

@description('Repository that runs the backup workflow, e.g. octocat/github-backup')
param githubRepo string
param githubBranch string = 'main'

@allowed(['Standard_LRS', 'Standard_ZRS', 'Standard_GRS', 'Standard_GZRS'])
param sku string = 'Standard_LRS'

@description('Days each backup is write-protected (0 = no retention policy)')
param retentionDays int = 30

@description('Days before backups are deleted by lifecycle management')
param keepDays int = 365

@description('Your Entra object ID, to list/restore from your machine (optional)')
param userObjectId string = ''

var blobContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: { name: sku }
  properties: {
    accessTier: 'Cool'
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: sa
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 14 }
    containerDeleteRetentionPolicy: { enabled: true, days: 14 }
  }
}

resource backups 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blob
  name: 'github-backups'
}

resource state 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blob
  name: 'backup-state'
}

resource worm 'Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-05-01' = if (retentionDays > 0) {
  parent: backups
  name: 'default'
  properties: {
    immutabilityPeriodSinceCreationInDays: retentionDays
    allowProtectedAppendWrites: false
  }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: sa
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'tier-and-expire'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['github-backups/github/'] }
            actions: {
              baseBlob: {
                tierToCold: { daysAfterCreationGreaterThan: 30 }
                delete: { daysAfterCreationGreaterThan: keepDays }
              }
            }
          }
        }
      ]
    }
  }
  dependsOn: [backups]
}

resource id 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-github-backup'
  location: location
}

resource fic 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: id
  name: 'github-${replace(githubRepo, '/', '-')}-${githubBranch}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubRepo}:ref:refs/heads/${githubBranch}'
    audiences: ['api://AzureADTokenExchange']
  }
}

resource idRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sa
  name: guid(sa.id, id.id, blobContributor)
  properties: {
    principalId: id.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: blobContributor
  }
}

resource userRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(userObjectId)) {
  scope: sa
  name: guid(sa.id, userObjectId, blobContributor)
  properties: {
    principalId: userObjectId
    principalType: 'User'
    roleDefinitionId: blobContributor
  }
}

output AZURE_CLIENT_ID string = id.properties.clientId
output AZURE_TENANT_ID string = subscription().tenantId
output GHB_STORAGE_ACCOUNT string = sa.name
