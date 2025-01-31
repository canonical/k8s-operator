# Terraform module for k8s-worker

This is a Terraform module facilitating the deployment of the k8s-worker charm, using the [Terraform juju provider](https://github.com/juju/terraform-provider-juju/). For more information, refer to the provider [documentation](https://registry.terraform.io/providers/juju/juju/latest/docs).

## Requirements

This module requires a `juju` model to be available. Refer to the [usage section](#usage) below for more details.

## API

### Inputs

The module offers the following configurable inputs:

| Name | Type | Description | Required | Default |
| - | - | - | - | - |
| `app_name`| string | Application name | False | k8s-worker |
| `base` | string | Ubuntu base to deploy the charm onto | False | ubuntu@24.04 |
| `channel`| string | Channel that the charm is deployed from | False | 1.30/edge |
| `config`| map(string) | Map of the charm configuration options | False | {} |
| `constraints` | string | Juju constraints to apply for this application | False | arch=amd64 |
| `model`| string | Name of the model that the charm is deployed on | True | - |
| `resources`| map(string) | Map of the charm resources | False | {} |
| `revision`| number | Revision number of the charm name | False | null |
| `units` | number | Number of units to deploy | False | 1 |

### Outputs

Upon applied, the module exports the following outputs:

| Name | Description |
| - | - |
| `app_name`|  Application name |
| `provides`| Map of `provides` endpoints |
| `requires`|  Map of `requires` endpoints |

## Usage

This module is intended to be used as part of a higher-level module. When defining one, users should ensure that Terraform is aware of the `juju_model` dependency of the charm module. There are two options to do so when creating a high-level module:

### Define a `juju_model` resource

Define a `juju_model` resource and pass to the `model_name` input a reference to the `juju_model` resource's name. For example:

```
resource "juju_model" "testing" {
  name = canonical-k8s
}
module "k8s_worker" {
  source = "<path-to-this-directory>"
  model = juju_model.testing.name
}
```

### Define a `data` source

Define a `data` source and pass to the `model_name` input a reference to the `data.juju_model` resource's name. This will enable Terraform to look for a `juju_model` resource with a name attribute equal to the one provided, and apply only if this is present. Otherwise, it will fail before applying anything.

```
data "juju_model" "testing" {
  name = var.model_name
}
module "k8s_worker" {
  source = "<path-to-this-directory>"
  model = data.juju_model.testing.name
}
```
