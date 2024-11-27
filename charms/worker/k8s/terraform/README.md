# Terraform module for k8s

This is a Terraform module facilitating the deployment of the k8s charm, using the [Terraform juju provider](https://github.com/juju/terraform-provider-juju/). For more information, refer to the provider [documentation](https://registry.terraform.io/providers/juju/juju/latest/docs).

## Requirements
This module requires a `juju` model to be available. Refer to the [usage section](#usage) below for more details.

## API

### Inputs
The module offers the following configurable inputs:

| Name | Type | Description | Required |
| - | - | - | - |
| `app_name`| string | Application name | False |
| `channel`| string | Channel that the charm is deployed from | False |
| `config`| map(string) | Map of the charm configuration options | False |
| `constriants` | string | Juju constraits to apply for this aplication | False |
| `model`| string | Name of the model that the charm is deployed on | True |
| `resources`| map(string) | Map of the charm resources | False |
| `revision`| number | Revision number of the charm name | False |
| `series` | string | Ubuntu series to deploy the carm onto | False |
| `units` | number | Number of units to deploy | False |

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
module "k8s" {
  source = "<path-to-this-directory>"
  model_name = juju_model.testing.name
}
```

### Define a `data` source
Define a `data` source and pass to the `model_name` input a reference to the `data.juju_model` resource's name. This will enable Terraform to look for a `juju_model` resource with a name attribute equal to the one provided, and apply only if this is present. Otherwise, it will fail before applying anything.
```
data "juju_model" "testing" {
  name = var.model_name
}
module "k8s" {
  source = "<path-to-this-directory>"
  model_name = data.juju_model.testing.name
}
```
