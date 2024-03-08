"""Provides ORM interaction for design builder."""
from types import FunctionType
from typing import Any, Dict, List, Mapping, Type, Union

from django.apps import apps
from django.db.models import Model, Manager
from django.db.models.fields import Field as DjangoField
from django.core.exceptions import ObjectDoesNotExist, ValidationError, MultipleObjectsReturned


from nautobot.core.graphql.utils import str_to_var_name
from nautobot.extras.models import JobResult, Relationship

from nautobot_design_builder import errors
from nautobot_design_builder import ext
from nautobot_design_builder.logging import LoggingMixin
from nautobot_design_builder.fields import field_factory


class Journal:
    """Keep track of the objects created or updated during the course of a design's implementation.

    The Journal provides a way to do post-implementation processing. For
    instance, if every item created in a design needs to be updated with
    a tag, then a post_implementation method can be created in the
    job and the journal.created items can be iterated and updated. The
    Journal contains three indices:

    index: a set of all the model UUIDs that have been created or updated

    created: a dictionary of objects created. The keys of this index are
    model classes and the values are sets of primary key UUIDs

    updated: like created, this index is a dictionary of objects that
    have been updated. The keys are model classes and the values are the primary
    key UUIDs

    An object's UUID may appear in both created and updated. However, they
    will only be in each of those indices at most once.
    """

    def __init__(self):
        """Constructor for Journal object."""
        self.index = set()
        self.created = {}
        self.updated = {}

    def log(self, model: "ModelInstance"):
        """Log that a model has been created or updated.

        Args:
            model (BaseModel): The model that has been created or updated
        """
        instance = model.instance
        model_type = instance.__class__
        if instance.pk not in self.index:
            self.index.add(instance.pk)

            if model._created:
                index = self.created
            else:
                index = self.updated

            index.setdefault(model_type, set())
            index[model_type].add(instance.pk)

    @property
    def created_objects(self) -> Dict[str, List[Model]]:
        """Return a dictionary of Nautobot objects that were created.

        Returns:
            Dict[str, List[BaseModel]]: A dictionary of created objects. The
            keys of the dictionary are the lower case content type labels
            (such as `dcim.device`) and the values are lists of created objects
            of the corresponding type.
        """
        results = {}
        for model_type, pk_list in self.created.items():
            object_list = []
            for primary_key in pk_list:
                instance = model_type.objects.get(pk=primary_key)
                object_list.append(instance)
            results[model_type._meta.label_lower] = object_list
        return results


def _map_query_values(query: Mapping) -> Mapping:
    retval = {}
    for key, value in query.items():
        if isinstance(value, ModelInstance):
            retval[key] = value.instance
        elif isinstance(value, Mapping):
            retval[key] = _map_query_values(value)
        else:
            retval[key] = value
    return retval


class ModelClass:
    name: str
    model_class: Type[Model]
    deferred = False

    def refresh_custom_relationships(self):
        for direction in Relationship.objects.get_for_model(self.model_class):
            for relationship in direction:
                field = field_factory(self, relationship)
                field.__set_name__(self, relationship.slug)
                setattr(self.__class__, relationship.slug, field)

    def __str__(self):
        return str(self.model_class)

    @classmethod
    def factory(cls, django_class: Type[Model]):
        cls_attributes = {
            "model_class": django_class,
            "name": django_class.__name__,
        }

        field: DjangoField
        for field in django_class._meta.get_fields():
            cls_attributes[field.name] = field_factory(None, field)
        model_class = type(django_class.__name__, (ModelInstance,), cls_attributes)
        return model_class


class ModelInstance(ModelClass):  # pylint: disable=too-many-instance-attributes
    """An individual object to be created or updated as Design Builder iterates through a rendered design YAML file."""

    # Action Definitions
    GET = "get"
    CREATE = "create"
    UPDATE = "update"
    CREATE_OR_UPDATE = "create_or_update"

    ACTION_CHOICES = [GET, CREATE, UPDATE, CREATE_OR_UPDATE]

    # Callback Event types
    PRE_SAVE = "PRE_SAVE"
    POST_INSTANCE_SAVE = "POST_INSTANCE_SAVE"
    POST_SAVE = "POST_SAVE"

    def __init__(
        self,
        creator: "Builder",
        attributes: dict,
        relationship_manager=None,
        parent=None,
    ):  # pylint:disable=too-many-arguments
        """Constructor for a ModelInstance."""
        self.creator = creator
        self.instance: Model = None
        # Make a copy of the attributes so the original
        # design attributes are not overwritten
        self.attributes = {**attributes}
        self._parent = parent
        self.signals = {
            self.PRE_SAVE: [],
            self.POST_INSTANCE_SAVE: [],
            self.POST_SAVE: [],
        }

        self._filter = {}
        self._action = None
        self._kwargs = {}
        self.refresh_custom_relationships()
        self._created = False
        self._parse_attributes()
        self.relationship_manager = relationship_manager
        if self.relationship_manager is None:
            self.relationship_manager = self.model_class.objects

        try:
            self._load_instance()
        except ObjectDoesNotExist as ex:
            raise errors.DoesNotExistError(self) from ex
        except MultipleObjectsReturned as ex:
            raise errors.MultipleObjectsReturnedError(self) from ex
        self._update_fields()

    def create_child(
        self,
        model_class: ModelClass,
        attributes: Dict,
        relationship_manager: Manager = None,
    ) -> "ModelInstance":
        """Create a new ModelInstance that is linked to the current instance.

        Args:
            model_class (Type[Model]): Class of the child model.
            attributes (Dict): Design attributes for the child.
            relationship_manager (Manager): Database relationship manager to use for the new instance.

        Returns:
            ModelInstance: Model instance that has its parent correctly set.
        """
        if not issubclass(model_class, ModelClass):
            model_class = self.creator.model_class_index[model_class]
        try:
            return model_class(
                self.creator,
                attributes,
                relationship_manager,
                parent=self,
            )
        except MultipleObjectsReturned:
            # pylint: disable=raise-missing-from
            raise errors.DesignImplementationError(
                f"Expected exactly 1 object for {model_class.__name__}({attributes}) but got more than one"
            )
        except ObjectDoesNotExist:
            query = ",".join([f'{k}="{v}"' for k, v in attributes.items()])
            # pylint: disable=raise-missing-from
            raise errors.DesignImplementationError(f"Could not find {model_class.__name__}: {query}")


    def _parse_attributes(self):  # pylint: disable=too-many-branches
        self._custom_fields = self.attributes.pop("custom_fields", {})
        attribute_names = list(self.attributes.keys())
        while attribute_names:
            key = attribute_names.pop(0)
            self.attributes[key] = self.creator.resolve_values(self.attributes[key])
            if key.startswith("!"):
                value = self.attributes.pop(key)
                args = key.lstrip("!").split(":")

                extn = self.creator.get_extension("attribute", args[0])
                if extn:
                    result = extn.attribute(*args[1:], value=value, model_instance=self)
                    if isinstance(result, tuple):
                        self.attributes[result[0]] = result[1]
                    elif isinstance(result, dict):
                        self.attributes.update(result)
                        attribute_names.extend(result.keys())
                    elif result is not None:
                        raise errors.DesignImplementationError(f"Cannot handle extension return type {type(result)}")
                elif args[0] in [self.GET, self.UPDATE, self.CREATE_OR_UPDATE]:
                    self._action = args[0]
                    self._filter[args[1]] = value

                    if self._action is None:
                        self._action = args[0]
                    elif self._action != args[0]:
                        raise errors.DesignImplementationError(
                            f"Can perform only one action for a model, got both {self._action} and {args[0]}",
                            self.model_class,
                        )
                else:
                    raise errors.DesignImplementationError(f"Unknown action {args[0]}", self.model_class)
            elif "__" in key:
                fieldname, search = key.split("__", 1)
                if not hasattr(self.model_class, fieldname):
                    raise errors.DesignImplementationError(f"{fieldname} is not a property", self.model_class)
                self.attributes[fieldname] = {f"!get:{search}": self.attributes.pop(key)}
            elif not hasattr(self, key):
                value = self.attributes.pop(key)
                if isinstance(value, ModelInstance):
                    value = value.instance
                self._kwargs[key] = value

        if self._action is None:
            self._action = self.CREATE
        if self._action not in self.ACTION_CHOICES:
            raise errors.DesignImplementationError(f"Unknown action {self._action}", self.model_class)

    def connect(self, signal: str, handler: FunctionType):
        """Connect a handler between this model instance (as sender) and signal.

        Args:
            signal (Signal): Signal to listen for.
            handler (FunctionType): Callback function
        """
        self.signals[signal].append(handler)

    def _send(self, signal: str):
        for handler in self.signals[signal]:
            handler()
            self.instance.refresh_from_db()

    def _load_instance(self):
        query_filter = _map_query_values(self._filter)
        field_values = {**self._filter}
        if self._action == self.GET:
            self.instance = self.model_class.objects.get(**query_filter)
            return

        if self._action in [self.UPDATE, self.CREATE_OR_UPDATE]:
            # perform nested lookups. First collect all the
            # query params for top-level relationships, then
            # perform the actual lookup
            for query_param in list(query_filter.keys()):
                if "__" in query_param:
                    value = query_filter.pop(query_param)
                    attribute, filter_param = query_param.split("__", 1)
                    query_filter.setdefault(attribute, {})
                    query_filter[attribute][f"!get:{filter_param}"] = value

            for query_param, value in query_filter.items():
                if isinstance(value, Mapping):
                    rel = getattr(self.model_class, query_param)
                    queryset = rel.get_queryset()

                    model = self.create_child(
                        self.creator.model_class_index[queryset.model],
                        value,
                        relationship_manager=queryset,
                    )
                    if model._action != self.GET:
                        model.save()
                    query_filter[query_param] = model.instance
                    field_values[query_param] = model
            try:
                self.instance = self.relationship_manager.get(**query_filter)
                return
            except ObjectDoesNotExist:
                if self._action == "update":
                    # pylint: disable=raise-missing-from
                    raise errors.DesignImplementationError(f"No match with {query_filter}", self.model_class)
                self._created = True
                # since the object was not found, we need to
                # put the search criteria back into the attributes
                # so that they will be set when the object is created
                self.attributes.update(field_values)
        elif self._action != "create":
            raise errors.DesignImplementationError(f"Unknown database action {self._action}", self.model_class)
        try:
            self.instance = self.model_class(**self._kwargs)
            self._created = True
        except TypeError as ex:
            raise errors.DesignImplementationError(str(ex), self.model_class)

    def _update_fields(self):  # pylint: disable=too-many-branches
        if self._action == self.GET and self.attributes:
            # TODO: wrap this up in a metadata field
            if len(self.attributes) != 1 and self.attributes[0] != "deferred":
                raise ValueError("Cannot update fields when using the GET action")

        for field_name, value in self.attributes.items():
            if hasattr(self.__class__, field_name):
                setattr(self, field_name, value)
            elif hasattr(self.instance, field_name):
                setattr(self.instance, field_name, value)

            for key, value in self._custom_fields.items():
                self.set_custom_field(key, value)

    def save(self):
        """Save the model instance to the database."""
        if self._action == self.GET:
            return

        self._send(ModelInstance.PRE_SAVE)

        msg = "Created" if self.instance._state.adding else "Updated"  # pylint: disable=protected-access
        try:
            self.instance.full_clean()
            self.instance.save()
            self._created = False
            # if self._parent is None:
            self.creator.log_success(message=f"{msg} {self.model_class.__name__} {self.instance}", obj=self.instance)
            self.creator.journal.log(self)
            # Refresh from DB so that we update based on any
            # post save signals that may have fired.
            self.instance.refresh_from_db()
        except ValidationError as validation_error:
            raise errors.DesignValidationError(self) from validation_error

        self._send(ModelInstance.POST_INSTANCE_SAVE)
        self._send(ModelInstance.POST_SAVE)

    def set_custom_field(self, field, value):
        """Sets a value for a custom field."""
        self.instance.cf[field] = value


# Don't add models from these app_labels to the
# object creator's list of top level models
_OBJECT_TYPES_APP_FILTER = set(
    [
        "django_celery_beat",
        "admin",
        "users",
        "django_rq",
        "auth",
        "taggit",
        "database",
        # "contenttypes",
        "sessions",
        "social_django",
    ]
)


class Builder(LoggingMixin):
    """Iterates through a design and creates and updates the objects defined within."""

    model_map: Dict[str, Type[Model]]
    model_class_index: Dict[Type, "ModelClass"]

    def __new__(cls, *args, **kwargs):
        """Sets the model_map class attribute when the first Builder initialized."""
        # only populate the model_map once
        if not hasattr(cls, "model_map"):
            cls.model_map = {}
            cls.model_class_index = {}
            for model_class in apps.get_models():
                if model_class._meta.app_label in _OBJECT_TYPES_APP_FILTER:
                    continue
                plural_name = str_to_var_name(model_class._meta.verbose_name_plural)
                cls.model_map[plural_name] = ModelClass.factory(model_class)
                cls.model_class_index[model_class] = cls.model_map[plural_name]
        return object.__new__(cls)

    def __init__(self, job_result: JobResult = None, extensions: List[ext.Extension] = None):
        """Constructor for Builder."""
        self.job_result = job_result

        self.extensions = {
            "extensions": [],
            "attribute": {},
            "value": {},
        }
        if extensions is None:
            extensions = []

        for extn_cls in [*extensions, *ext.extensions()]:
            if not issubclass(extn_cls, ext.Extension):
                raise errors.DesignImplementationError("{extn_cls} is not an action tag extension.")

            extn = {
                "class": extn_cls,
                "object": None,
            }
            if issubclass(extn_cls, ext.AttributeExtension):
                self.extensions["attribute"][extn_cls.tag] = extn
            if issubclass(extn_cls, ext.ValueExtension):
                self.extensions["value"][extn_cls.tag] = extn

            self.extensions["extensions"].append(extn)

        self.journal = Journal()

    def get_extension(self, ext_type: str, tag: str) -> ext.Extension:
        """Looks up an extension based on its tag name and returns an instance of that Extension type.

        Args:
            ext_type (str): the type of the extension, i.e. 'attribute' or 'value'
            tag (str): the tag used for the extension, i.e. 'ref' or 'git_context'

        Returns:
            Extension: An instance of the Extension class
        """
        extn = self.extensions[ext_type].get(tag)
        if extn is None:
            return None

        if extn["object"] is None:
            extn["object"] = extn["class"](self)
        return extn["object"]

    def implement_design(self, design: Dict, commit: bool = False):
        """Iterates through items in the design and creates them.

        If either commit=False (default) or an exception is raised, then any extensions
        with rollback functionality are called to revert their state. If commit=True
        and no exceptions are raised then the extensions with commit functionality are
        called to finalize changes.

        Args:
            design (Dict): An iterable mapping of design changes.
            commit (bool): Whether or not to commit the transaction. Defaults to False.

        Raises:
            DesignImplementationError: if the model is not in the model map
        """
        if not design:
            raise errors.DesignImplementationError("Empty design")

        try:
            for key, value in design.items():
                if key in self.model_map and value:
                    self._create_objects(self.model_map[key], value)
                else:
                    raise errors.DesignImplementationError(f"Unknown model key {key} in design")
            if commit:
                self.commit()
            else:
                self.roll_back()
        except Exception as ex:
            self.roll_back()
            raise ex

    def resolve_value(self, value):
        """Resolve a value using extensions, if needed."""
        if isinstance(value, str) and value.startswith("!"):
            (action, arg) = value.lstrip("!").split(":", 1)
            extn = self.get_extension("value", action)
            if extn:
                value = extn.value(arg)
            else:
                raise errors.DesignImplementationError(f"Unknown attribute extension {value}")
        return value

    def resolve_values(self, value: Union[list, dict, str]) -> Any:
        """Resolve a value, or values, using extensions.

        Args:
            value (Union[list,dict,str]): The value to attempt to resolve.

        Returns:
            Any: The resolved value.
        """
        if isinstance(value, str):
            value = self.resolve_value(value)
        elif isinstance(value, list):
            # copy the list so we don't change the input
            value = list(value)
            for i, item in enumerate(value):
                value[i] = self.resolve_value(item)
        elif isinstance(value, dict):
            # copy the dict so we don't change the input
            value = dict(value)
            for k, item in value.items():
                value[k] = self.resolve_value(item)
        return value

    def _create_objects(self, model_cls, objects):
        if isinstance(objects, dict):
            model = model_cls(self, objects)
            model.save()
        elif isinstance(objects, list):
            for model_instance in objects:
                model = model_cls(self, model_instance)
                model.save()

    def commit(self):
        """Method to commit all changes to the database."""
        for extn in self.extensions["extensions"]:
            if hasattr(extn["object"], "commit"):
                extn["object"].commit()

    def roll_back(self):
        """Looks for any extensions with a roll back method and executes it.

        Used for for rolling back changes that can't be undone with a database rollback, for example config context files.

        """
        for extn in self.extensions["extensions"]:
            if hasattr(extn["object"], "roll_back"):
                extn["object"].roll_back()
