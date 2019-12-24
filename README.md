<h1><p align="center"> PyWren for IBM Cloud </p></h1>


### What is PyWren
[PyWren](https://github.com/pywren/pywren) is an open source project whose goals are massively scaling the execution of Python code and its dependencies on serverless computing platforms and monitoring the results. PyWren delivers the user’s code into the serverless platform without requiring knowledge of how functions are invoked and run. 

### PyWren and IBM Cloud
This repository is based on [PyWren](https://github.com/pywren/pywren) main branch and adapted for IBM Cloud Functions and IBM Cloud Object Storage. IBM-PyWren is not, however, just a mere reimplementation of PyWren’s API atop IBM Cloud Functions. Rather, it is must be viewed as an advanced extension of PyWren to run broader Map-Reduce jobs, based on Docker images. In extending PyWren to work with IBM Cloud Object Storage, we also added a partition discovery component that allows PyWren to process large amounts of data stored in the IBM Cloud Object Storage. See [changelog](CHANGELOG.md) for more details.

PyWren - IBM provides great value for the variety of uses cases, like processing data in object storage, running embarrassingly parallel compute jobs (e.g. Monte-Carlo simulations), enriching data with additional attributes and many more


This document describes the steps to use PyWren over IBM Cloud Functions and IBM Cloud Object Storage.

### IBM Cloud for Academic institutions
[IBM Academic Initiative](https://ibm.biz/academic) is a special program that allows free trial of IBM Cloud for Academic institutions. This program is provided for students and faculty staff members, and allow up to 12 months of free usage. You can register your university email and get a free of charge account.


# Getting Started
1. [Initial requirements](#initial-requirements)
2. [PyWren setup](#pywren-setup)
3. [Verify - Unit Testing](#verify-unit-testing)
4. [How to use PyWren for IBM Cloud](#how-to-use-pywren-for-ibm-cloud)
   - [Functions](#functions)
   - [Using PyWren to process data from IBM Cloud Object Storage and public URLs](#using-pywren-to-process-data-from-ibm-cloud-object-storage-and-public-urls)
   - [PyWren on IBM Watson Studio and Jupyter notebooks](#pywren-on-ibm-watson-studio-and-jupyter-notebooks)
5. [Additional resources](#additional-resources)


## Initial Requirements
* IBM Cloud Functions account, as described [here](https://cloud.ibm.com/openwhisk/). Make sure you can run end-to-end example with Python.
* IBM Cloud Object Storage [account](https://www.ibm.com/cloud/object-storage)
* Python 3.5, Python 3.6 or Python 3.7


## PyWren Setup

First, install PyWren from the PyPi repository:

	pip install pywren-ibm-cloud

Then, configure the client with the access details to your IBM Cloud Object Storage and IBM Cloud Functions accounts. You can find the complete instructions and all the available configuration keys [here](config/).

Once installed an configured, you can test PyWren by simply copy-pasting the next code:

```python
import pywren_ibm_cloud as pywren

def add_seven(x):
    return x + 7

if __name__ == '__main__':
    ibmcf = pywren.ibm_cf_executor()
    ibmcf.call_async(add_seven, 3)
    print(ibmcf.get_result())
```

PyWren automatically deploys the default runtime, based on the Python version you are using, the first time you execute a function. Additionally, you can build your custom runtimes with the libraries that your functions depend on. Check more information about runtimes [here](runtime/).


## Verify - Unit Testing

To test that all is working, use the command:

    python -m pywren_ibm_cloud.tests

Notice that if you didn't set a local PyWren's config file, you need to provide it as a json file path by `-c <CONFIG>` flag. 

Alternatively, for debugging purposes, you can run specific tests by `-t <TESTNAME>`. use `--help` flag to get more information about the test script.


## How to use PyWren for IBM Cloud
The primary object in PyWren is the executor. The standard way to get everything set up is to import pywren_ibm_cloud, and call one of the available methods to get a ready-to-use executor. 

The available executors are:
- `ibm_cf_executor()`: IBM Cloud Functions executor.
- `knative_executor()`: Knative executor.
- `openwhisk_executor()`: Vanilla OpenWhisk executor.
- `function_executor()`: Generic executor based on the compute_backend specified in configuration.
- `local_executor()`: Localhost executor to run functions by using local processes.

The available methods within an executor are:

|API Call| Type | Description|
|---|---|---|
|call_async() | Async. | Method used to spawn one function activation |
|map() | Async. | Method used to spawn multiple function activations |
|map_reduce() | Async. | Method used to spawn multiple function activations with one (or multiple) reducers|
|wait() | Sync. | Wait for the function activations to complete. It blocks the local execution until all the function activations finished their execution (configurable)|
|get_result() | Sync. | Method used to retrieve the results of all function activations. The results are returned within an ordered list, where each element of the list is the result of one activation|
|create_execution_plots() | Sync. | Method used to create execution plots |
|clean() | Async. | Method used to clean the temporary data generated by PyWren in IBM COS |

For additional information and examples check the complete [API details](docs/api-details.md).

### Functions
PyWren for IBM Cloud allows sending multiple parameters in each function invocation. See detailed examples [here](docs/multiple-parameters.md). Moreover, multiple parameters in functions allowed us to add some new built-in capabilities in PyWren. Thus, take into account that there are some reserved parameter names that activate internal logic. These reserved parameters are:

- **id**: To get the call id. For instance, if you spawn 10 activations of a function, you will get here a number from 0 to 9, for example: [map.py](examples/map.py)

- **ibm_cos**: To get a ready-to use [ibm_boto3.Client()](https://ibm.github.io/ibm-cos-sdk-python/reference/services/s3.html#client) instance. This allows you to access your IBM COS account from any function in an easy way, for example: [ibmcos_arg.py](examples/ibmcos_arg.py)

- **rabbitmq**: To get a ready-to use [pika.BlockingConnection()](https://pika.readthedocs.io/en/0.13.1/modules/adapters/blocking.html) instance (AMQP URL must be set in the [configuration](config/) to make it working). This allows you to access the RabbitMQ service from any function in an easy way, for example: [rabbitmq_arg.py](examples/rabbitmq_arg.py)

- **obj** & **url**: These two parameters activate internal logic that allows processing data objects stored in the IBM Cloud Object Storage service or public URLs in a transparent way. Read the following section that provides full details and instructions on how to use this built-in data-processing logic.


### Using PyWren to process data from IBM Cloud Object Storage and public URLs
PyWren for IBM Cloud functions has built-in logic for processing data objects from public URLs and IBM Cloud Object Storage. When you write in the parameters of a function the parameter name: **obj**, you are telling to PyWren that you want to process objects located in IBM Cloud Object Storage service. In contrast, when you write in the parameters of a function the parameter name: **url**, you are telling to PyWren that you want to process data from publicly accessible URLs. 

Additionally, the built-in data-processing logic integrates a **data partitioner** system that allows to automatically split the dataset in smallest chunks. Navigate into [docs/data-processing.md](docs/data-processing.md) to see the complete details about data processing in PyWren.


### PyWren on IBM Watson Studio and Jupyter notebooks
It is possible to use **IBM-PyWren** inside **IBM Watson Studio** or Jupyter notebooks in order to run your workloads. You must ensure that the **IBM-PyWren** package is installed in the environment you are using the notebook. To do so, if you can't install the package manually, we recommend to add these lines at the beginning of the notebook:

```python
import sys
try:
    import pywren_ibm_cloud as pywren
except:
    !{sys.executable} -m pip install pywren-ibm-cloud
    import pywren_ibm_cloud as pywren
```
Installation supports PyWren version as an input parameter, for example:

	!{sys.executable} -m pip install -U pywren-ibm-cloud==1.3.0

Once installed, you can use IBM-PyWren as usual inside the notebook. See an example in [hello_world.ipynb](examples/hello_world.ipynb). Don't forget of the [configuration](config/).


## Additional resources

* [Your easy move to serverless computing and radically simplified data processing](https://conferences.oreilly.com/strata/strata-ny/public/schedule/detail/77226) Strata Data Conference, NY 2019
  * See video of PyWren-IBM usage [here](https://www.youtube.com/watch?v=EYa95KyYEtg&list=PLpR7f3Www9KCjYisaG7AMaR0C2GqLUh2G&index=3&t=0s) and the example of Monte Carlo [here](https://www.youtube.com/watch?v=vF5HI2q5VKw&list=PLpR7f3Www9KCjYisaG7AMaR0C2GqLUh2G&index=2&t=0s)
* [Ants, serverless computing, and simplified data processing](https://developer.ibm.com/blogs/2019/01/31/ants-serverless-computing-and-simplified-data-processing/)
* [Speed up data pre-processing with PyWren in deep learning](https://developer.ibm.com/patterns/speed-up-data-pre-processing-with-pywren-in-deep-learning/)
* [Predicting the future with Monte Carlo simulations over IBM Cloud Functions](https://www.ibm.com/blogs/bluemix/2019/01/monte-carlo-simulations-with-ibm-cloud-functions/)
* [Process large data sets at massive scale with PyWren over IBM Cloud Functions](https://www.ibm.com/blogs/bluemix/2018/04/process-large-data-sets-massive-scale-pywren-ibm-cloud-functions/)
* [PyWren for IBM Cloud on CODAIT](https://developer.ibm.com/code/open/centers/codait/projects/pywren/)
* [Industrial project in Technion on PyWren-IBM](http://www.cs.technion.ac.il/~cs234313/projects_sites/W19/04/site/)
* [Serverless data analytics in the IBM Cloud](https://dl.acm.org/citation.cfm?id=3284029) - Proceedings of the 19th International Middleware Conference (Industry)