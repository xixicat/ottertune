import logging
import pdb

from collections import OrderedDict
from pytz import timezone

from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404, HttpResponse, QueryDict
from django.shortcuts import redirect, render, get_object_or_404
from django.template.context_processors import csrf
from django.template.defaultfilters import register
from django.urls import reverse, reverse_lazy
from django.utils.datetime_safe import datetime
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from djcelery.models import TaskMeta

from .forms import ApplicationForm, NewResultForm, ProjectForm
from .models import (Application, BenchmarkConfig, DBConf, DBMSCatalog,
                     DBMSMetrics, Hardware, KnobCatalog, MetricCatalog, PipelineResult, Project, Result,
                     ResultData, Statistics, WorkloadCluster)
from tasks import aggregate_target_results, map_workload, configuration_recommendation
from .types import DBMSType, KnobUnitType, MetricType, PipelineTaskType, StatsType, TaskType, VarType
from .utils import DBMSUtil, JSONUtil, LabelUtil, MediaUtil
from website.types import HardwareType

log = logging.getLogger(__name__)


# For the html template to access dict object
@register.filter
def get_item(dictionary, key):
    return dictionary.get(key)


def ajax_new(request):
    new_id = request.GET['new_id']
    ts = Statistics.objects.filter(data_result=new_id,
                                   type=StatsType.SAMPLES)
    data = {}
    metric_meta = Statistics.objects.metric_meta
    for metric, metric_info in metric_meta.iteritems():
        if len(ts) > 0:
            offset = ts[0].time
            if len(ts) > 1:
                offset -= ts[1].time - ts[0].time
            data[metric] = []
            for t in ts:
                data[metric].append(
                    [t.time - offset,
                        getattr(t, metric) * metric_info.scale])
    return HttpResponse(JSONUtil.dumps(data), content_type='application/json')


def signup_view(request):
    if request.user.is_authenticated():
        return redirect(reverse('home'))
#         return redirect('/')
    if request.method == 'POST':
        post = request.POST
        form = UserCreationForm(post)
        if form.is_valid():
            form.save()
            new_post = QueryDict(mutable=True)
            new_post.update(post)
            new_post['password'] = post['password1']
            request.POST = new_post
            return login_view(request)
        else:
            log.warn(form.is_valid())
            log.warn(form.errors)
    else:
        form = UserCreationForm()
    token = {}
    token.update(csrf(request))
    token['form'] = form

    return render(request, 'signup.html', token)


def login_view(request):
    if request.user.is_authenticated():
        return redirect(reverse('home'))
#         return redirect('/')
    if request.method == 'POST':
        post = request.POST
        form = AuthenticationForm(None, post)
        if form.is_valid():
            login(request, form.get_user())
            return redirect(reverse('home'))
#             return redirect('/')
        else:
            log.info("Invalid request: {}".format(
                ', '.join(form.error_messages)))
    else:
        form = AuthenticationForm()
    token = {}
    token.update(csrf(request))
    token['form'] = form

    return render(request, 'login.html', token)


@login_required(login_url=reverse_lazy('login'))
def logout_view(request):
    logout(request)
    return redirect(reverse('login'))


@login_required(login_url=reverse_lazy('login'))
def redirect_home(request):
    return redirect(reverse('home'))

@login_required(login_url=reverse_lazy('login'))
def home(request):
    labels = Project.get_labels()
    labels.update(LabelUtil.style_labels({
        'button_create': 'create a new project',
        'button_delete': 'delete selected projects',
    }))
    labels['title'] = 'Your Projects'
    context = {
        "projects": Project.objects.filter(user=request.user),
        "labels": labels
    }
    context.update(csrf(request))
    return render(request, 'home.html', context)


def get_task_status(tasks):
    if len(tasks) == 0:
        return None, 0
    overall_status = 'SUCCESS'
    num_completed = 0
    for task in tasks:
        status = task.status
        if status == "SUCCESS":
            num_completed += 1
        elif status in ['FAILURE', 'REVOKED', 'RETRY']:
            overall_status = status
            break
        else:
            assert status in ['PENDING', 'RECEIVED', 'STARTED']
            overall_status = status
    return overall_status, num_completed


@login_required(login_url=reverse_lazy('login'))
def ml_info(request, project_id, app_id, result_id):
#     result_id = request.GET['id']
    res = Result.objects.get(pk=result_id)

    task_ids = res.task_ids.split(',')
    tasks = []
    for tid in task_ids:
        task = TaskMeta.objects.filter(task_id=tid).first()
        if task is not None:
            tasks.append(task)

    overall_status, num_completed = get_task_status(tasks)
    if overall_status in ['PENDING', 'RECEIVED', 'STARTED']:
        completion_time = 'N/A'
        total_runtime = 'N/A'
    else:
        completion_time = tasks[-1].date_done
        total_runtime = (completion_time - res.creation_time).total_seconds()
        total_runtime = '{0:.2f} seconds'.format(total_runtime)

    task_info = [(tname, task) for tname, task in \
                 zip(TaskType.TYPE_NAMES.values(), tasks)]

    context = {"id": result_id,
               "result": res,
               "overall_status": overall_status,
               "num_completed": "{} / {}".format(num_completed, 3),
               "completion_time": completion_time,
               "total_runtime": total_runtime,
               "tasks": task_info}

    return render(request, "ml_info.html", context)


@login_required(login_url=reverse_lazy('login'))
def project(request, project_id):
#     project_id = request.GET['id']
    applications = Application.objects.filter(project=project_id)
    project = Project.objects.get(pk=project_id)
    labels = Application.get_labels()
    labels.update(LabelUtil.style_labels({
        'button_delete': 'delete selected applications',
        'button_create': 'create a new application',
    }))
    labels['title'] = "Your Applications"
    context = {
        "applications": applications,
        "project": project,
        "labels": labels,
        }
    context.update(csrf(request))
    return render(request, 'home_application.html', context)


@login_required(login_url=reverse_lazy('login'))
def application(request, project_id, app_id):
    project = get_object_or_404(Project, pk=project_id)
    app = get_object_or_404(Application, pk=app_id)
#     app_id = Application.objects.get(pk=request.GET['id'])
    if project.user != request.user:
        return render(request, '404.html')
#     project = app.project
    data = request.GET
    results = Result.objects.filter(application=app)
    dbs = {}
    benchmarks = {}

    for res in results:
        dbs[res.dbms.key] = res.dbms
        bench_type = res.benchmark_config.benchmark_type
        if bench_type not in benchmarks:
            benchmarks[bench_type] = set()
        benchmarks[bench_type].add(res.benchmark_config)

    benchmarks = {k: sorted(list(v)) for k, v in benchmarks.iteritems()}
    benchmarks = OrderedDict(sorted(benchmarks.iteritems()))

    lastrevisions = [10, 50, 100]
    dbs = OrderedDict(sorted(dbs.items()))
#     filters = []
#     for field in BenchmarkConfig.FILTER_FIELDS:
#         value_dict = {}
#         for res in results:
#             value_dict[getattr(res.benchmark_conf, field['field'])] = True
#         f = {'values': [key for key in value_dict.iterkeys()],
#              'print': field['print'], 'field': field['field']}
#         filters.append(f)

    if len(benchmarks) > 0:
        default_bench, default_confs = benchmarks.iteritems().next()
        default_confs = ','.join([str(c.pk) for c in default_confs])
    else:
        default_bench = 'show_none'
        default_confs = 'none'

    default_metrics = Statistics.objects.default_metrics
    if app.target_objective not in default_metrics:
        default_metrics.append(app.target_objective)

    labels = Application.get_labels()
    labels['title'] = "Application Info"
    metric_meta = Statistics.objects.metric_meta
    context = {
        'project': project,
        'dbmss': dbs,
        'benchmarks': benchmarks,
        'lastrevisions': lastrevisions,
        'defaultdbms': app.dbms.key,
        'defaultlast': 10,
        'defaultequid': "on",
        'defaultbenchmark': default_bench,
        'defaultspe': default_confs,
        'metrics': metric_meta.keys(),
        'metric_meta': metric_meta,
        'defaultmetrics': default_metrics,
        'filters': [],
        'application': app,
        'results': results,
        'labels': labels,
    }

    context.update(csrf(request))
    return render(request, 'application.html', context)


# @login_required(login_url=reverse_lazy('login'))
# def edit_project(request):
#     context = {}
#     try:
#         if request.GET['id'] != '':
#             project = Project.objects.get(pk=request.GET['id'])
#             if project.user != request.user:
#                 return render(request, '404.html')
#             context['project'] = project
#             context['labels'] = Project.get_labels()
#     except Project.DoesNotExist:
#         pass
#     return render(request, 'edit_project.html', context)


@login_required(login_url=reverse_lazy('login'))
def delete_project(request):
    for pk in request.POST.getlist('projects', []):
        project = Project.objects.get(pk=pk)
        if project.user == request.user:
            project.delete()
    return redirect(reverse('home'))


@login_required(login_url=reverse_lazy('login'))
def delete_application(request):
    for pk in request.POST.getlist('applications', []):
        application = Application.objects.get(pk=pk)
        if application.user == request.user:
            application.delete()
    return redirect(reverse('delete_application'))


@login_required(login_url=reverse_lazy('login'))
def update_project(request, project_id=''):
    if request.method == 'POST':
        if project_id == '':
            form = ProjectForm(request.POST)
            if not form.is_valid():
                return HttpResponse(str(form))
            project = form.save(commit=False)
            project.user = request.user
            ts = now()
            project.creation_time = ts
            project.last_update = ts
            project.save()
        else:
            project = Project.objects.get(pk=int(project_id))
            if project.user != request.user:
                return Http404()
            form = ProjectForm(request.POST, instance=project)
            if not form.is_valid():
                return HttpResponse(str(form))
            project.last_update = now()
            project.save()
        return redirect(reverse('project', kwargs={'project_id': project.pk}))
    else:
        if project_id == '':
            project = None
            form = ProjectForm()
        else:
            project = Project.objects.get(pk=int(project_id))
            form = ProjectForm(instance=project)
        context = {
            'project': project,
            'form': form,
        }
        return render(request, 'edit_project.html', context)


@login_required(login_url=reverse_lazy('login'))
def update_application(request, project_id, app_id=''):
    project = get_object_or_404(Project, pk=project_id)
    if request.method == 'POST':
        if project.user != request.user:
            return Http404()
        if app_id == '':
            form = ApplicationForm(request.POST)
            if not form.is_valid():
                return HttpResponse(str(form))
            app = form.save(commit=False)
            app.user = request.user
            app.project = project
            ts = now()
            app.creation_time = ts
            app.last_update = ts
            app.upload_code = MediaUtil.upload_code_generator()
            app.save()
        else:
            app = Application.objects.get(pk=int(app_id))
            form = ApplicationForm(request.POST, instance=app)
            if not form.is_valid():
                return HttpResponse(str(form))
            if form.cleaned_data['gen_upload_code'] is True:
                app.upload_code = MediaUtil.upload_code_generator()
            app.last_update = now()
            app.save()
        return redirect(reverse('application', kwargs={'project_id': project_id, 'app_id': app.pk}))
#         return redirect('/application/?id=' + str(app.pk))
    else:
#         project = get_object_or_404(Project, pk=project_id)
        if project.user != request.user:
            return Http404()
        if app_id != '':
            app = Application.objects.get(pk=app_id)
            form = ApplicationForm(instance=app)
        else:
            app = None
            form = ApplicationForm(
                initial={
                    'dbms': DBMSCatalog.objects.get(
                        type=DBMSType.POSTGRES, version='9.6'),
                    'hardware': Hardware.objects.get(
                        type=HardwareType.EC2_M3XLARGE),
                    'target_objective': Statistics.objects.P99_LATENCY,
                })
        context = {
            'project': project,
            'application': app,
            'form': form,
        }
        return render(request, 'edit_application.html', context)


@csrf_exempt
def new_result(request):
    if request.method == 'POST':
        form = NewResultForm(request.POST, request.FILES)

        if not form.is_valid():
            log.warning("Form is not valid:\n" + str(form))
            return HttpResponse("Form is not valid\n" + str(form))
        upload_code = form.cleaned_data['upload_code']
        cluster_name = form.cleaned_data['cluster_name']
        try:
            application = Application.objects.get(upload_code=upload_code)
        except Application.DoesNotExist:
            log.warning("Wrong upload code: " + upload_code)
            return HttpResponse("wrong upload_code!")

        return handle_result_files(application, request.FILES, cluster_name)
    log.warning("Request type was not POST")
    return HttpResponse("POST please\n")


def handle_result_files(app, files, cluster_name):
    from celery import chain

    # Load summary file and verify that the database/version is supported
    summary = JSONUtil.loads(''.join(files['summary_data'].chunks()))
    dbms_type = DBMSType.type(summary['DBMS Type'])
    # FIXME! bad hack until I have time to get the PG 9.3 metric/knob data in
    # the same form
    dbms_version = "9.6"
#     dbms_version = DBMSUtil.parse_version_string(
#        dbms_type, summary['DBMS Version'])

    try:
        dbms_object = DBMSCatalog.objects.get(
            type=dbms_type, version=dbms_version)
    except ObjectDoesNotExist:
        return HttpResponse('{} v{} is not yet supported.'.format(
            summary['DBMS Type'], dbms_version))

    if dbms_object != app.dbms:
        return HttpResponse('The DBMS must match the type and version '
                            'specified when creating the application. '
                            '(expected=' + app.dbms.full_name + ') '
                            '(actual=' + dbms_object.full_name + ')')

    # Load parameters, metrics, benchmark, and samples
    db_parameters = JSONUtil.loads(
        ''.join(files['db_parameters_data'].chunks()))
    db_metrics = JSONUtil.loads(''.join(files['db_metrics_data'].chunks()))
    benchmark_config_str = ''.join(files['benchmark_conf_data'].chunks())
    samples = ''.join(files['sample_data'].chunks())

    benchmark_config = BenchmarkConfig.objects.create_benchmark_config(
        app, benchmark_config_str, summary['Benchmark Type'].upper())

    db_conf_dict, db_diffs = DBMSUtil.parse_dbms_config(
        dbms_object.pk, db_parameters)
    db_conf = DBConf.objects.create_dbconf(
        app, JSONUtil.dumps(db_conf_dict, pprint=True, sort=True),
        JSONUtil.dumps(db_diffs), dbms_object)

    db_metrics_dict, met_diffs = DBMSUtil.parse_dbms_metrics(
            dbms_object.pk, db_metrics)
    dbms_metrics = DBMSMetrics.objects.create_dbms_metrics(
        app, JSONUtil.dumps(db_metrics_dict, pprint=True, sort=True),
        JSONUtil.dumps(met_diffs), benchmark_config.time, dbms_object)

    timestamp = datetime.fromtimestamp(
        int(summary['Current Timestamp (milliseconds)']) / 1000,
        timezone("UTC"))
    result = Result.objects.create_result(
        app, dbms_object, benchmark_config, db_conf, dbms_metrics,
        JSONUtil.dumps(summary, pprint=True, sort=True), samples,
        timestamp)
    result.summary_stats = Statistics.objects.create_summary_stats(
        summary, result, benchmark_config.time)
    result.save()
    Statistics.objects.create_sample_stats(samples, result)

    wkld_cluster = WorkloadCluster.objects.create_workload_cluster(
        dbms_object, app.hardware, cluster_name)
    param_data = DBMSUtil.convert_dbms_params(
        dbms_object.pk, db_conf_dict)
    external_metrics = Statistics.objects.get_external_metrics(summary)
    metric_data = DBMSUtil.convert_dbms_metrics(
        dbms_object.pk, db_metrics_dict, external_metrics,
        int(benchmark_config.time))

    ResultData.objects.create(result=result,
                              cluster=wkld_cluster,
                              param_data=JSONUtil.dumps(param_data,
                                                        pprint=True,
                                                        sort=True),
                              metric_data=JSONUtil.dumps(metric_data,
                                                         pprint=True,
                                                         sort=True))

    nondefault_settings = DBMSUtil.get_nondefault_settings(dbms_object.pk,
                                                           db_conf_dict)
    app.project.last_update = now()
    app.last_update = now()
    if app.nondefault_settings is None:
        app.nondefault_settings = JSONUtil.dumps(nondefault_settings)
    app.project.save()
    app.save()

    path_prefix = MediaUtil.get_result_data_path(result.pk)
    paths = [
        (path_prefix + '.samples', 'sample_data'),
        (path_prefix + '.summary', 'summary_data'),
        (path_prefix + '.params', 'db_parameters_data'),
        (path_prefix + '.metrics', 'db_metrics_data'),
        (path_prefix + '.expconfig', 'benchmark_conf_data'),
    ]

    for path, content_name in paths:
        with open(path, 'w') as f:
            for chunk in files[content_name].chunks():
                f.write(chunk)

    if 'raw_data' in files:
        with open('{}.csv.tgz'.format(path_prefix), 'w') as f:
            for chunk in files['raw_data'].chunks():
                f.write(chunk)

    if app.tuning_session is False:
        return HttpResponse("Store success!")

    response = chain(aggregate_target_results.s(result.pk),
                     map_workload.s(),
                     configuration_recommendation.s()).apply_async()
    taskmeta_ids = [response.parent.parent.id, response.parent.id, response.id]
    result.task_ids = ','.join(taskmeta_ids)
    result.save()
    return HttpResponse("Store Success! Running tuner... (status={})".format(
        response.status))


def filter_db_var(kv_pair, key_filters):
    for f in key_filters:
        if f.match(kv_pair[0]):
            return True
    return False


@login_required(login_url=reverse_lazy('login'))
def db_conf_ref(request, dbms_name, version, param_name):
    param = get_object_or_404(KnobCatalog, dbms__type=DBMSType.type(dbms_name), dbms__version=version, name=param_name)
    labels = KnobCatalog.get_labels()
    list_items = OrderedDict()
    if param.category is not None:
        list_items[labels['category']] = param.category
    list_items[labels['scope']] = param.scope
    list_items[labels['tunable']] = param.tunable
    list_items[labels['vartype']] = VarType.name(param.vartype)
    if param.unit != KnobUnitType.OTHER:
        list_items[labels['unit']] = param.unit
    list_items[labels['default']] = param.default
    if param.minval is not None:
        list_items[labels['minval']] = param.minval
    if param.maxval is not None:
        list_items[labels['maxval']] = param.maxval
    if param.enumvals is not None:
        list_items[labels['enumvals']] = param.enumvals
    if param.summary is not None:
        description = param.summary
        if param.description is not None:
            description += param.description
        list_items[labels['summary']] = description
    
    context = {
        'title': param.name,
        'dbms': param.dbms,
        'is_used': param.tunable,
        'used_label': 'TUNABLE', #labels['tunable'],
        'list_items': list_items,
    }
    return render(request, 'dbms_reference.html', context)


@login_required(login_url=reverse_lazy('login'))
def db_metrics_ref(request, dbms_name, version, metric_name):
    metric = get_object_or_404(MetricCatalog, dbms__type=DBMSType.type(dbms_name), dbms__version=version, name=metric_name)
    labels = MetricCatalog.get_labels()
    list_items = OrderedDict()
    list_items[labels['scope']] = metric.scope
    list_items[labels['vartype']] = VarType.name(metric.vartype)
    list_items[labels['summary']] = metric.summary
    context = {
        'title': metric.name,
        'dbms': metric.dbms,
        'is_used': metric.metric_type == MetricType.COUNTER,
        'used_label': MetricType.name(metric.metric_type), #labels['tunable'],
        'list_items': list_items,
    }
    return render(request, 'dbms_reference.html', context=context)


@login_required(login_url=reverse_lazy('login'))
def db_conf_view(request, project_id, app_id, dbconf_id):
    db_info = get_object_or_404(DBConf, pk=dbconf_id)
    if db_info.application.user != request.user:
        raise Http404()
    labels = DBConf.get_labels()
    labels.update(LabelUtil.style_labels({
        'featured_info': 'tunable dbms parameters',
        'all_info': 'all dbms parameters',
    }))
    labels['title'] = 'DBMS Configuration'
    context = {
        'labels': labels,
        'info_type': 'db_confs'
    }
    return db_info_view(request, context, db_info)


@login_required(login_url=reverse_lazy('login'))
def db_metrics_view(request, project_id, app_id, dbmet_id, compare=None):
    db_info = get_object_or_404(DBMSMetrics, pk=dbmet_id)
    if db_info.application.user != request.user:
        raise Http404()
    labels = DBMSMetrics.get_labels()
    labels.update(LabelUtil.style_labels({
        'featured_info': 'numeric dbms metrics',
        'all_info': 'all dbms metrics',
    }))
    labels['title'] = 'DBMS Metrics'
    context = {
        'labels': labels,
        'info_type': 'db_metrics'
    }
    return db_info_view(request, context, db_info)


def db_info_view(request, context, db_info):
    if context['info_type'] == 'db_confs':
        model_class = DBConf
        filter_fn = DBMSUtil.filter_tunable_params
        addl_args = []
    else:
        model_class = DBMSMetrics
        filter_fn = DBMSUtil.filter_numeric_metrics
        addl_args = [True]

    dbms_id = db_info.dbms.pk
    all_info_dict = JSONUtil.loads(db_info.configuration)
    args = [dbms_id, all_info_dict] + addl_args
    featured_dict = filter_fn(*args)

    if 'compare' in request.GET and request.GET['compare'] != 'none':
#     if compare != None:
        comp_id = request.GET['compare']
        compare_obj = model_class.objects.get(pk=comp_id)
        comp_dict = JSONUtil.loads(compare_obj.configuration)
        args = [dbms_id, comp_dict] + addl_args
        comp_featured_dict = filter_fn(*args)

        all_info = [(k, v, comp_dict[k]) for k, v in all_info_dict.iteritems()]
        featured_info = [(k, v, comp_featured_dict[k])
                         for k, v in featured_dict.iteritems()]
    else:
        comp_id = None
        all_info = list(all_info_dict.iteritems())
        featured_info = list(featured_dict.iteritems())
    peer_info = model_class.objects.filter(
        dbms=db_info.dbms, application=db_info.application)
    peer_info = filter(lambda x: x.pk != db_info.pk, peer_info)

    context['all_info'] = all_info
    context['featured_info'] = featured_info
    context['db_info'] = db_info
    context['compare'] = comp_id
    context['peer_db_info'] = peer_info
    return render(request, 'db_info.html', context)
    
        

@login_required(login_url=reverse_lazy('login'))
def benchmark_configuration(request, project_id, app_id, bench_id):
    benchmark_conf = get_object_or_404(BenchmarkConfig, pk=bench_id)
    app = benchmark_conf.application
    if app.user != request.user:
        return render(request, '404.html')

    db_confs = DBConf.objects.filter(dbms=app.dbms,
                                     application=benchmark_conf.application)
    all_db_confs = []
    conf_map = {}
    for conf in db_confs:
        results = Result.objects.filter(application=app,
                                        dbms_config=conf,
                                        benchmark_config=benchmark_conf)
        if len(results) == 0:
            continue
        result = results.latest('timestamp')
        all_db_confs.append(conf.pk)
        conf_map[conf.name] = [conf, result]
    if len(conf_map) > 0:
        dbs = { app.dbms.full_name: conf_map}
    else:
        dbs = {}

    labels = BenchmarkConfig.get_labels()
    labels['title'] = 'Benchmark Configuration'
    context = {'benchmark': benchmark_conf,
               'dbs': dbs,
               'metric_meta': Statistics.objects.metric_meta,
               'default_dbconf': all_db_confs,
               'default_metrics': ['throughput', 'p99_latency'],
               'labels': labels,
               'proj_id': project_id,
               'app_id': app_id}
    return render(request, 'benchmark_conf.html', context)

# Data Format
#    error
#    metrics as a list of selected metrics
#    results
#        data for each selected metric
#            meta data for the metric
#            Result list for the metric in a folded list


@login_required(login_url=reverse_lazy('login'))
def get_benchmark_data(request):
    data = request.GET

    benchmark_conf = get_object_or_404(BenchmarkConfig, pk=data['id'])
    app = benchmark_conf.application
    if app.user != request.user:
        return render(request, '404.html')

    results = Result.objects.filter(benchmark_config=benchmark_conf)
    results = sorted(results, cmp=lambda x,
                     y: int(y.summary_stats.throughput - x.summary_stats.throughput))

    metrics = data.get('met')
    if metrics is not None:
        metrics = metrics.split(',')
    else:
        metrics = Statistics.objects.default_metrics
    if app.tuning_session and app.target_objective not in metrics:
        metrics.append(app.target_objective)
        
    data_package = {'results': [],
                    'error': 'None',
                    'metrics': metrics}
    for met in data_package['metrics']:
        met_info = Statistics.objects.get_meta(met)
        data_package['results'].append({'data': [[]], 'tick': [],
                                        'unit': met_info.unit,
                                        'lessisbetter': met_info.improvement,
                                        'metric': met_info.pprint})

        added = {}
        db_confs = data['db'].split(',')
        i = len(db_confs)
        for r in results:
            if r.dbms_config.pk in added or str(r.dbms_config.pk) not in db_confs:
                continue
            added[r.dbms_config.pk] = True
            data_package['results'][-1]['data'][0].append([
                i, getattr(r.summary_stats, met) * met_info.scale,
                r.pk, getattr(r.summary_stats, met) * met_info.scale])
            data_package['results'][-1]['tick'].append(r.dbms_config.name)
            i -= 1
        data_package['results'][-1]['data'].reverse()
        data_package['results'][-1]['tick'].reverse()

    return HttpResponse(JSONUtil.dumps(data_package), content_type='application/json')


@login_required(login_url=reverse_lazy('login'))
def get_benchmark_conf_file(request):
    bench_id = request.GET['id']
    benchmark_conf = get_object_or_404(BenchmarkConfig, pk=request.GET['id'])
    if benchmark_conf.application.user != request.user:
        return render(request, '404.html')

    response = HttpResponse(benchmark_conf.configuration,
                            content_type='text/plain')
    response['Content-Disposition'] = 'attachment; filename=result_' + \
        str(bench_id) + '.ben.cnf'
    return response


@login_required(login_url=reverse_lazy('login'))
def edit_benchmark_conf(request):
    context = {}
    if request.GET['id'] != '':
        ben_conf = get_object_or_404(BenchmarkConfig, pk=request.GET['id'])
        if ben_conf.application.user != request.user:
            return render(request, '404.html')
        context['benchmark'] = ben_conf
    return render(request, 'edit_benchmark.html', context)


@login_required(login_url=reverse_lazy('login'))
def update_benchmark_conf(request, project_id, app_id, bench_id):
    bench_conf = get_object_or_404(BenchmarkConfig, pk=bench_id)
    if request.method == "POST":
        bench_conf.name = request.POST['name']
        bench_conf.description = request.POST['description']
        bench_conf.save()
        return redirect(reverse('bench_conf', kwargs={ 'project_id': project_id, 'app_id': app_id, 'bench_id': bench_id }))
    else:
        context['benchmark'] = ben_conf
    return render(request, 'edit_benchmark.html', context)

def result_similar(a, b, compare_params):
#     ranked_knobs = JSONUtil.loads(PipelineResult.get_latest(
#         dbms_id, hw_id, PipelineTaskType.RANKED_KNOBS).value)[:10]
    dbms_id = a.dbms.pk
    db_conf_a = DBMSUtil.filter_tunable_params(
        dbms_id, JSONUtil.loads(a.dbms_config.configuration))
    db_conf_b = DBMSUtil.filter_tunable_params(
        dbms_id, JSONUtil.loads(b.dbms_config.configuration))
    for param in compare_params:
        if db_conf_a[param] != db_conf_b[param]:
            return False
    return True


def result_same(a, b):
    dbms_id = a.dbms.pk
    db_conf_a = DBMSUtil.filter_tunable_params(
        dbms_id, JSONUtil.loads(a.dbms_config.configuration))
    db_conf_b = DBMSUtil.filter_tunable_params(
        dbms_id, JSONUtil.loads(b.dbms_config.configuration))
    for k, v in db_conf_a.iteritems():
        if k not in db_conf_b or v != db_conf_b[k]:
            return False
    return True


# def result_same(a, b):
#     db_conf_a = JSONUtil.loads(a.dbms_config.configuration)
#     db_conf_b = JSONUtil.loads(b.dbms_config.configuration)
#     for k, v in db_conf_a.iteritems():
#         if k not in db_conf_b or v != db_conf_b[k]:
#             return False
#     return True


@login_required(login_url=reverse_lazy('login'))
def update_similar(request):
    raise Http404()

@login_required(login_url=reverse_lazy('login'))
def result(request, project_id, app_id, result_id):
    target = get_object_or_404(Result, pk=result_id)
    app = target.application
    if app.user != request.user:
        raise Http404()
    data_package = {}
    results = Result.objects.filter(application=target.application,
                                    dbms=app.dbms,
                                    benchmark_config=target.benchmark_config)
    same_dbconf_results = filter(
        lambda x: x.pk != target.pk and result_same(x, target), results)
    ranked_knobs = JSONUtil.loads(PipelineResult.get_latest(
        app.dbms, app.hardware, PipelineTaskType.RANKED_KNOBS).value)[:10]
    similar_dbconf_results = filter(
        lambda x: x.pk not in \
        ([target.pk] + [r.pk for r in same_dbconf_results]) and \
        result_similar(x, target, ranked_knobs), results)

    metric_meta = Statistics.objects.metric_meta
    for metric, metric_info in metric_meta.iteritems():
        data_package[metric] = {
            'data': {},
            'units': metric_info.unit,
            'lessisbetter': metric_info.improvement,
            'metric': metric_info.pprint,
            'print': metric_info.pprint,
        }

        same_id = [str(target.pk)]
        for x in same_id:
            key = metric + ',data,' + x
            tmp = cache.get(key)
            if tmp is not None:
                data_package[metric]['data'][int(x)] = []
                data_package[metric]['data'][int(x)].extend(tmp)
                continue

            ts = Statistics.objects.filter(data_result=x, type=StatsType.SAMPLES)
            if len(ts) > 0:
                offset = ts[0].time
                if len(ts) > 1:
                    offset -= ts[1].time - ts[0].time
                data_package[metric]['data'][int(x)] = []
                for t in ts:
                    data_package[metric]['data'][int(x)].append(
                        [t.time - offset, getattr(t, metric) * metric_info.scale])
                cache.set(key, data_package[metric]['data'][int(x)], 60 * 5)

    default_metrics = {}
    for met in Statistics.objects.default_metrics:
        default_metrics[met] = (getattr(target.summary_stats, met) *
                                Statistics.objects.get_meta(metric).scale)

    status = None
    if target.task_ids is not None:
        task_ids = target.task_ids.split(',')
        tasks = []
        for tid in task_ids:
            task = TaskMeta.objects.filter(task_id=tid).first()
            if task is not None:
                tasks.append(task)
        status, _ = get_task_status(tasks)
        if status is None:
            status = 'UNAVAILABLE'

    next_conf_available = True if status == 'SUCCESS' else False
    labels = Result.get_labels()
    labels.update(LabelUtil.style_labels({
        'sampled_data': 'sampled data',
        'raw_data': 'raw data',
        'status': 'status',
        'next_conf_available': 'next configuration'
    }))
    labels['title'] = 'Result Info'
    context = {
        'result': target,
        'metric_meta': metric_meta,
        'default_metrics': default_metrics,
        'data': JSONUtil.dumps(data_package),
        'same_runs': same_dbconf_results,
        'status': status,
        'next_conf_available': next_conf_available,
        'similar_runs': similar_dbconf_results,
        'labels': labels,
        'project_id': app.project.pk,
        'app_id': app.pk
    }
    return render(request, 'result.html', context)


@login_required(login_url=reverse_lazy('login'))
def get_result_data_file(request):
    target = get_object_or_404(Result, pk=request.GET['id'])
    if target.application.user != request.user:
        return render(request, '404.html')

    result_id = int(request.GET['id'])
    result_type = request.GET['type']

    prefix = MediaUtil.get_result_data_path(result_id)

    if result_type == 'samples':
        filepath = prefix + '.samples'
    elif result_type == 'raw':
        filepath = prefix + '.raw'
    elif result_type == 'next_conf':
        filepath = prefix + '.next_conf'
    return MediaUtil.download_file(filepath)


# Data Format:
#    error
#    results
#        all result data after the filters for the table
#    timelines
#        data for each benchmark & metric pair
#            meta data for the pair
#            data as a map<DBMS name, result list>
@login_required(login_url=reverse_lazy('login'))
def get_timeline_data(request):
    result_labels = Result.get_labels()
    table_metrics = [Statistics.objects.THROUGHPUT, Statistics.objects.P99_LATENCY]
    columnnames = [
        result_labels['id'],
        result_labels['creation_time'],
        result_labels['dbms_config'],
        result_labels['dbms_metrics'],
        result_labels['benchmark_config'],
    ]
    for met in table_metrics:
        met_info = Statistics.objects.get_meta(met)
        columnnames.append(
            met_info.pprint + ' (' + 
            met_info.short_unit + ')') 

    data_package = {
        'error': 'None',
        'timelines': [], 
        'columnnames': columnnames,
    }

    application = get_object_or_404(Application, pk=request.GET['app'])
    if application.user != request.user:
        return HttpResponse(JSONUtil.dumps(data_package), content_type='application/json')

    revs = int(request.GET['revs'])

    # Get all results related to the selected DBMS, sort by time
    results = Result.objects.filter(application=application)
#     results = filter(lambda x: x.dbms.key in request.GET[
#                      'db'].split(','), results)
    results = sorted(results, cmp=lambda x, y: int(
        (x.timestamp - y.timestamp).total_seconds()))

    default_metrics = Statistics.objects.default_metrics
    if application.tuning_session is True and \
            application.target_objective not in default_metrics:
        default_metrics.append(application.target_objective)
    display_type = request.GET['ben']
    if display_type == 'show_none':
        benchmarks = []
        metrics = default_metrics
        results = []
        pass
#     elif display_type == 'grid':
#         metrics = default_metrics
#         benchmarks = set()
#         benchmark_confs = []
#         for result in results:
#             benchmarks.add(result.benchmark_config.benchmark_type)
#             benchmark_confs.append(result.benchmark_config)
#         benchmarks = list(benchmarks)
    else:
        metrics = request.GET.get(
            'met', ','.join(default_metrics)).split(',')
        benchmarks = [display_type]
        benchmark_confs = filter(lambda x: x != '', request.GET[
                                 'spe'].strip().split(','))
        results = filter(lambda x: str(x.benchmark_config.pk)
                         in benchmark_confs, results)
    
    result_list = []
    for x in results:
        entry = [
            x.pk,
            x.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            x.dbms_config.name,
            x.dbms_metrics.name,
            x.benchmark_config.name]
        for met in table_metrics:
            entry.append(getattr(x.summary_stats, met) *
                         Statistics.objects.get_meta(met).scale)
        entry.extend([
            x.dbms_config.pk,
            x.dbms_metrics.pk,
            x.benchmark_config.pk
        ])
        result_list.append(entry)
    data_package['results'] = result_list

    # For plotting charts
    for metric in metrics:
        met_info = Statistics.objects.get_meta(metric)
        for bench in benchmarks:
            b_r = filter(
                lambda x: x.benchmark_config.benchmark_type == bench, results)
            if len(b_r) == 0:
                continue

            data = {
                'benchmark': bench,
                'units': met_info.unit,
                'lessisbetter': met_info.improvement,
                'data': {},
                'baseline': "None",
                'metric': metric,
                'print_metric': met_info.pprint,
            }

            for db in request.GET['db'].split(','):
                d_r = filter(lambda x: x.dbms.key == db, b_r)
                d_r = d_r[-revs:]
                out = [
                    [
                        res.timestamp.strftime("%m-%d-%y %H:%M"),
                        getattr(res.summary_stats, metric) * met_info.scale,
                        "",
                        str(res.pk)
                    ]
                    for res in d_r]

                if len(out) > 0:
                    data['data'][db] = out

            data_package['timelines'].append(data)

    return HttpResponse(JSONUtil.dumps(data_package), content_type='application/json')
